"""ScreenTableProcessor — a screen-level table processor built on the same base
modules as the statement pipeline, but driving its own per-screen LLM loop.

Android only perceives the visible viewport, so real table UI is a cursor-window
interaction: see a screen -> judge it -> act on it -> advance. This processor
implements that loop by reusing the statement pipeline's foundation:

- perception:  ``AndroidPerception.observe()`` -> Observation
- context:     ``build_observation_view`` + ``project_transition_frame`` +
               ``assemble_messages`` (capacity-managed prompt + image)
- decision:    ``invoke_structured`` with a screen-level schema (not transition.md)
- execute:     ``ActionExecutor`` / ``AndroidExecutor.execute`` (normalized 0-1000
               coords, settle orchestration)
- advance:     ``move_collection`` (bound-anchored scroll)

It deliberately does NOT go through ``supervisor.step`` / the statement transition
pipeline — each screen gets one LLM decision with the structured rows as context,
and the executor resolves exact coordinates from the row structure.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from gui_agent.context.blocks import ContextBlock
from gui_agent.core.llm.messages import assemble_messages
from gui_agent.core.run.collection_view import collection_candidates
from gui_agent.core.schemas import Observation, StatementContract
from gui_agent.core.supervisor.statement.context_projection import (
    project_transition_frame,
)
from gui_agent.core.supervisor.statement.context_variants import transition_frame_block
from gui_agent.core.supervisor.statement.observation_view import build_observation_view


class ScreenRowAction(BaseModel):
    """A row on the current screen that matches the foreach goal."""

    row: str = Field(description="the row's name / identity matching the target")
    reason: str = Field(default="", description="why this row matches the target")
    x: float | None = Field(
        default=None,
        description="normalized x 0-1000 of the row/card (required when the "
                    "structured list has no coordinate, e.g. a feed card)",
    )
    y: float | None = Field(
        default=None,
        description="normalized y 0-1000 of the row/card (required when the "
                    "structured list has no coordinate, e.g. a feed card)",
    )


class ScreenDecision(BaseModel):
    """One screen-level LLM decision for a foreach invocation."""

    assessment: str = Field(default="", description="one-sentence judgment of this screen")
    matched_rows: list[ScreenRowAction] = Field(
        default_factory=list,
        description="rows on this screen that match the goal target",
    )
    scroll: str = Field(
        default="none", description="up | down | none | done — advance to next screen"
    )
    done: bool = Field(default=False, description="the whole table is processed")


class ScreenActionPlan(BaseModel):
    """The LLM's pre-traversal plan: how to accomplish the foreach action on
    THIS interface (used as context for every subsequent screen decision)."""

    analysis: str = Field(
        default="", description="how the current interface supports the action"
    )
    steps: list[str] = Field(
        default_factory=list, description="concrete steps to accomplish the action"
    )
    mode_required: str = Field(
        default="", description="e.g. '管理勾选模式' if the interface needs a mode switch"
    )


def _screen_block(block_id: str, content: str, *, source: str = "screen_table") -> ContextBlock:
    """Build a screen-level context block (unified prompt assembly)."""
    return ContextBlock(
        id=f"screen_table.{block_id}",
        budget="required",
        source_type="decision_frame",
        source=source,
        ttl="turn",
        priority=20,
        content=content,
    )


class ScreenTableProcessor:
    """Drive a screen-at-a-time table loop, reusing the statement base modules."""

    def __init__(
        self,
        bundle: Any,
        platform: Any,
        log_dir: Path,
        *,
        entity: str = "",
        max_screens: int = 15,
    ) -> None:
        self.bundle = bundle
        self.platform = platform
        self.log_dir = log_dir
        self.entity = entity
        self.max_screens = max_screens
        self._perception = bundle.make_perception(platform, log_dir / "screen.png")
        self._executor = bundle.make_executor(platform)
        self.processed_rows: set[str] = set()
        self.read_results: list[dict] = []
        self._plan: ScreenActionPlan | None = None

    # ── public entry ────────────────────────────────────────────────────────────

    def foreach(self, state: Any, target: str, action: str) -> dict:
        """Iterate a table, acting on every row matching ``target`` with ``action``.

        ``target`` is the goal description the LLM matches rows against
        (e.g. "短袖T恤衬衫"); ``action`` is the free-text action to perform on
        each matched row (e.g. "删除" or "勾选后删除"). Each screen the LLM
        decides which rows match, the executor performs ``action`` on them, then
        the loop advances to the next screen until the table is traversed.
        """
        self.target = target
        self.action = action
        self.processed_rows.clear()
        self.read_results.clear()
        # Pre-traversal: have the LLM think about how THIS interface accomplishes
        # the action (e.g. deletion needs a manage/checkbox mode). The plan becomes
        # context for every subsequent screen decision.
        try:
            self._plan = self._plan_action(self._observe(), target, action)
        except Exception:
            self._plan = None
        log: list[dict] = []
        stable_empty = 0
        seen_names: set[str] = set()
        no_progress = 0
        for screen_no in range(1, self.max_screens + 1):
            obs = self._observe()
            rows = self._rows(obs)
            visible = [r for r in rows if r["buttons"]]
            if not visible:
                stable_empty += 1
                if stable_empty >= 2:
                    break
                self._advance(obs)
                continue
            stable_empty = 0
            new_names = [r["name"] for r in visible if r["name"] not in seen_names]
            seen_names.update(r["name"] for r in visible)
            decision = self._decide_screen(obs, visible)
            log.append({"screen": screen_no, "rows": len(visible), "decision": decision.model_dump()})
            acted = False
            for matched in decision.matched_rows:
                if matched.row in self.processed_rows:
                    continue
                self._execute_row(obs, matched)
                self.processed_rows.add(matched.row)
                acted = True
            if decision.done:
                break
            # Terminate when scrolling reveals no new rows and no NEW action was
            # taken this screen — the table has been fully traversed.
            if not new_names and not acted:
                no_progress += 1
                if no_progress >= 2:
                    break
            else:
                no_progress = 0
            if decision.scroll in {"down", "up"}:
                self._advance(obs, direction=decision.scroll)
            else:
                self._advance(obs)
        # After matching + acting on rows, execute the plan's interface-level
        # finish steps (e.g. click "删除选中" then confirm in the dialog).
        finish_results: list[str] = []
        if self.processed_rows and self._plan is not None:
            finish_results = self._execute_plan_finish()
        return {
            "screens": log,
            "processed_rows": sorted(self.processed_rows),
            "read_results": list(self.read_results),
            "finish_results": finish_results,
        }

    def _execute_plan_finish(self) -> list[str]:
        """Execute the plan's interface-level finish steps (non-row actions).

        After all matched rows have been acted on (e.g. checkboxes selected),
        the plan's remaining steps are interface-level actions such as clicking
        the "删除选中" button and confirming in a dialog. Each is located by the
        LLM looking at the current screen and tapped via the platform executor.
        """
        if self._plan is None:
            return []
        results: list[str] = []
        # Row-level select steps are already done; keep interface-level steps
        # (mentioning 删除选中 / 确定 / 确认 / 完成 / 进入管理模式).
        interface_steps = [
            s for s in self._plan.steps
            if any(kw in s for kw in ("删除选中", "确定", "确认", "完成", "管理模式", "勾选模式"))
        ]
        for step in interface_steps:
            point = self._locate_interface_button(step)
            if point is None:
                results.append(f"SKIP {step}: no button located")
                continue
            from gui_agent.core.schemas import BaseAction, BaseActionDecision

            action = BaseAction(
                action_type="tap", x=point[0], y=point[1],
                description=step,
            )
            decision = BaseActionDecision(action=action)
            try:
                obs = self._observe()
                self._executor.execute(
                    decision,
                    app_name="", png_bytes=obs.png_bytes,
                    is_home_screen=False, target_control="",
                )
                results.append(f"OK {step}")
            except Exception as exc:  # noqa: BLE001
                results.append(f"FAIL {step}: {exc}")
        return results

    def _locate_interface_button(self, step: str) -> tuple[float, float] | None:
        """Ask the LLM (with the current screenshot) where a named interface
        button is, returning normalized 0-1000 coordinates."""
        from llm.structured import invoke_structured

        from gui_agent.core.supervisor.statement.model_io import _make_llm

        class _Point(BaseModel):
            x: float = Field(description="normalized x 0-1000")
            y: float = Field(description="normalized y 0-1000")

        obs = self._observe()
        prompt = (
            f"看截图，找到按钮『{step}』的位置。"
            f"输出该按钮中心的归一化坐标(0-1000)。如果截图上看不到，x/y 输出 -1。"
        )
        messages = assemble_messages(
            "定位界面按钮",
            obs,
            system_blocks=[],
            human_blocks=[_screen_block("locate", prompt)],
            label="screen_table_locate",
        )
        try:
            point = invoke_structured(_make_llm(), messages, _Point, trace_label="locate")
            if point.x < 0 or point.y < 0:
                return None
            return (point.x, point.y)
        except Exception:
            return None

    # ── perception ──────────────────────────────────────────────────────────────

    def _observe(self) -> Observation:
        return self._perception.observe()

    def _rows(self, obs: Observation) -> list[dict]:
        """Rows visible in the current screen: name + action button coordinates.

        Uses the structured projection (collection_candidates) only. When the
        structured path finds nothing (feed/grid without uiautomator coords),
        the LLM decides target cards directly from the screenshot — the decision
        already carries the image, so a separate visual row pass is redundant.
        """
        return self._rows_structured(obs)

    def _rows_structured(self, obs: Observation) -> list[dict]:
        candidates = collection_candidates(obs)
        region = next(
            (c for c in candidates if c.get("projection") == "cells"),
            candidates[0] if candidates else None,
        )
        if region is None:
            return []
        rows: list[dict] = []
        for cell in region.get("table", {}).get("_collection_cells") or []:
            name = self._cell_name(cell)
            if not name:
                continue
            rows.append({
                "name": name,
                "cell": cell,
                "buttons": self._cell_buttons(cell, obs),
            })
        return rows

    def _cell_name(self, cell: dict) -> str:
        texts = cell.get("texts") or []
        for text in texts:
            token = str(text or "").strip()
            if not token:
                continue
            if token.startswith(("unsplash", "photo", "B0", "B09")) or token in {
                "￥", ".00", "S码", "M码", "L码", "标准款", "标准版", "-", "+", "1", "删除",
                "白色", "黑色", "蓝色", "红色", "绿色", "灰色", "黄色", "粉色", "紫色",
            }:
                continue
            if token.replace("_", "").isalnum() and "_" in token:
                continue
            return token
        return ""

    def _cell_buttons(self, cell: dict, obs: Observation) -> dict:
        """Map a cell's controls to named button coordinates (normalized 0-1000).

        The first control (empty label) is the select/checkbox; labeled ones are
        quantity/delete/edit. Coordinates come from the semantic tree by ref.
        """
        sem = {
            n.get("ref"): n for n in (obs.semantic_tree or []) if n.get("ref") and n.get("point")
        }
        result: dict[str, tuple[float, float]] = {}
        controls = cell.get("controls") or []
        for index, control in enumerate(controls):
            if not isinstance(control, dict):
                continue
            node = sem.get(control.get("ref")) or {}
            point = node.get("point") or {}
            x, y = point.get("x"), point.get("y")
            if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
                continue
            coord = (float(x), float(y))
            label = str(control.get("label") or "")
            if index == 0 and not label:
                result["select"] = coord
            elif label:
                result[label] = coord
        return result

    # ── decision (reuse statement's context assembly + LLM invocation) ─────────

    def _plan_action(self, obs: Observation, target: str, action: str) -> ScreenActionPlan:
        """Have the LLM think about how THIS interface accomplishes the action.

        The plan (steps + required mode) is produced once before traversal and
        injected as context into every screen decision, so the per-screen LLM
        knows e.g. "deletion requires entering the manage/checkbox mode first"
        instead of blindly tapping an invisible row delete button.
        """
        from llm.structured import invoke_structured

        from gui_agent.core.supervisor.statement.model_io import _make_llm
        from gui_agent.prompts import load_prompt_text

        rows = self._rows(obs)
        visible = [r for r in rows if r["buttons"]]
        rows_text = "\n".join(
            f"- {r['name']} [actions: {', '.join(r['buttons'].keys()) or 'none'}]"
            for r in visible
        )
        prompt = load_prompt_text("task.execution.screen_table_plan")
        task_text = (
            f"目标行动：对匹配 {target!r} 的行执行 {action!r}\n\n"
            f"请**看截图**判断这个界面如何完成目标行动。注意："
            f"UIAutomator 可能报告界面上实际看不到的按钮（如滑动删除层），"
            f"请以截图上真实可见的交互为准。\n\n"
            f"当前可见行（结构化参考，坐标由执行器确定）：\n{rows_text}\n\n"
            f"请分析该界面完成目标行动的真实步骤（含是否需要进入管理模式/勾选模式）："
        )
        messages = assemble_messages(
            prompt,
            obs,
            system_blocks=[],
            human_blocks=[_screen_block("plan", task_text)],
            label="screen_table_plan",
        )
        return invoke_structured(
            _make_llm(), messages, ScreenActionPlan, trace_label="screen_table_plan",
        )

    def _decide_screen(self, obs: Observation, rows: list[dict]) -> ScreenDecision:
        from llm.structured import invoke_structured

        from gui_agent.core.run.statement_memory import build_memory_view
        from gui_agent.core.supervisor.statement.model_io import _make_llm
        from gui_agent.prompts import load_prompt_text

        contract = StatementContract(
            id="screen-table",
            goal=f"{self.target}，采取行动：{self.action}",
            success="the table rows matching the goal are processed",
            persistence="immediate",
        )
        memory = build_memory_view(
            instance_id="screen-table",
            contract=contract,
            history=[],
            observation=obs,
        )
        view = build_observation_view(contract, obs, [])
        frame = project_transition_frame(
            contract, obs, memory, view, initial_filters={},
        )
        rows_text = "\n".join(
            f"- {row['name']} [actions: {', '.join(row['buttons'].keys()) or 'none'}]"
            for row in rows
        )
        # When the structured list is empty (feed/grid without uiautomator
        # coordinates), the LLM decides target cards directly from the screenshot
        # and must supply each matched card's center in x/y.
        structured_missing = not rows
        # A compact human block describing the target + action + current screen
        # rows; the observation image is attached by assemble_messages.
        processed = self.processed_rows
        frame["screen"] = {
            "target": self.target,
            "action": self.action,
            "rows": rows_text,
            "already_processed": sorted(processed),
            "structured_missing": structured_missing,
            # NOTE: the pre-traversal plan is deliberately NOT injected into the
            # per-screen matching decision — it distracts the LLM from row
            # matching. The plan is used only at EXECUTION time (which button to
            # tap, what interface-level finish steps to run).
        }
        prompt = load_prompt_text("task.execution.screen_table")
        messages = assemble_messages(
            prompt,
            obs,
            system_blocks=[transition_frame_block(frame)],
            human_blocks=[],
            label="screen_table",
        )
        decision = invoke_structured(
            _make_llm(), messages, ScreenDecision, trace_label="screen_table",
        )
        return decision

    # ── execution ───────────────────────────────────────────────────────────────

    def _execute_row(self, obs: Observation, matched: ScreenRowAction) -> str:
        """Execute the foreach ``action`` on one matched row.

        The action is free text (e.g. "删除" / "勾选后删除" / "读取"). Read-like
        actions ("读取"/"收集"/"查看"/"read") return the row's structured data
        (name + cell texts) into ``self.read_results`` instead of tapping. Other
        actions map to a row button by keyword and tap it through the platform
        executor (normalized -> device pixels).
        """
        row = next(
            (r for r in self._rows(obs) if r["name"] == matched.row), None
        )
        if row is None:
            # Structured list has no coordinate for this row (feed/grid): the LLM
            # decision carries the visual center, so use it as an open point.
            if matched.x is not None and matched.y is not None:
                row = {
                    "name": matched.row,
                    "cell": None,
                    "buttons": {"open": (matched.x, matched.y)},
                }
            else:
                return f"row {matched.row!r} not in current screen"
        # Detail actions take precedence over read actions: "打开详情，读取价格"
        # opens the detail page and reads there, not the list row.
        if self._is_detail_action(self.action):
            return self._execute_detail(obs, row, matched)
        if self._is_read_action(self.action):
            texts = (row.get("cell") or {}).get("texts") or []
            self.read_results.append({
                "name": matched.row,
                "texts": list(texts),
            })
            return f"READ {matched.row}"
        buttons = row.get("buttons") or {}
        coord = self._action_coord(self.action, buttons, self._plan)
        if coord is None:
            return f"no button for action {self.action!r} on {matched.row!r}"
        # Drive the device through the platform executor (normalized -> pixels).
        # Use the shared BaseAction/BaseActionDecision — this module is in the
        # core layer and must not import adapter-specific action types.
        from gui_agent.core.schemas import BaseAction, BaseActionDecision

        action = BaseAction(
            action_type="tap", x=coord[0], y=coord[1],
            description=f"{self.action} {matched.row}",
        )
        decision = BaseActionDecision(action=action)
        try:
            self._executor.execute(
                decision,
                app_name="",
                png_bytes=obs.png_bytes,
                is_home_screen=False,
                target_control=matched.row,
            )
            return f"OK {self.action} {matched.row}"
        except Exception as exc:  # noqa: BLE001
            return f"failed: {exc}"

    @staticmethod
    def _is_read_action(action: str) -> bool:
        return any(
            kw in action for kw in ("读取", "收集", "查看", "读出", "read", "collect")
        )

    @staticmethod
    def _is_detail_action(action: str) -> bool:
        """An action that opens the row's detail page and operates there.

        Detected by detail-opening keywords (打开/详情/进入/enter/detail/open).
        """
        return any(
            kw in action for kw in ("详情", "打开", "进入", "detail", "enter", "open")
        )

    def _execute_detail(self, obs: Observation, row: dict, matched: ScreenRowAction) -> str:
        """Open the row's detail page and let the LLM operate there.

        1. Tap the row body to open its detail page.
        2. Perceive the detail page; the LLM looks at it and performs the rest of
           the action (read a field, tap a button, add to cart, ...).
        3. Return to the list (back) so traversal can continue.
        """
        # 1. Tap the row body to open the detail page.
        coord = row.get("buttons", {}).get("select") or row.get("buttons", {}).get("open")
        if coord is None and row.get("bounds"):
            b = row["bounds"]
            coord = ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)  # row center
        if coord is None:
            return f"no point to open {matched.row!r}"
        from gui_agent.core.schemas import BaseAction, BaseActionDecision

        open_action = BaseAction(
            action_type="tap", x=coord[0], y=coord[1],
            description=f"open {matched.row} detail",
        )
        try:
            self._executor.execute(
                BaseActionDecision(action=open_action),
                app_name="", png_bytes=obs.png_bytes,
                is_home_screen=False, target_control=matched.row,
            )
        except Exception as exc:  # noqa: BLE001
            return f"open detail failed: {exc}"

        # 2. Perceive the detail page; LLM performs the remaining action.
        import time
        time.sleep(1.0)
        detail_obs = self._observe()
        detail_result = self._operate_detail(detail_obs, matched)

        # 3. Return to the list so traversal can continue.
        self._go_back()
        return detail_result

    def _operate_detail(self, detail_obs: Observation, matched: ScreenRowAction) -> str:
        """Ask the LLM to perform the action's remaining steps on the detail page.

        The LLM sees the detail screenshot + the action, decides taps/reads.
        """
        from llm.structured import invoke_structured

        from gui_agent.core.supervisor.statement.model_io import _make_llm

        class _DetailDecision(BaseModel):
            assessment: str = Field(default="", description="what the detail page shows")
            taps: list[dict] = Field(
                default_factory=list,
                description="buttons to tap: [{description, x, y}] normalized 0-1000",
            )
            notes: str = Field(default="", description="observations / read values")

        prompt_text = (
            f"已进入商品 {matched.row!r} 的详情页。目标行动：{self.action}\n"
            f"看截图，输出要在详情页执行的动作。taps 为要点击的按钮坐标(归一化0-1000)；"
            f"notes 记录读取到的信息。"
        )
        messages = assemble_messages(
            "详情页操作",
            detail_obs,
            system_blocks=[],
            human_blocks=[_screen_block("detail", prompt_text)],
            label="screen_table_detail",
        )
        try:
            decision = invoke_structured(
                _make_llm(), messages, _DetailDecision, trace_label="screen_table_detail",
            )
            executed: list[str] = []
            for tap in decision.taps:
                x, y = tap.get("x"), tap.get("y")
                if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
                    continue
                from gui_agent.core.schemas import BaseAction, BaseActionDecision

                self._executor.execute(
                    BaseActionDecision(action=BaseAction(
                        action_type="tap", x=float(x), y=float(y),
                        description=tap.get("description", "detail tap"),
                    )),
                    app_name="", png_bytes=detail_obs.png_bytes,
                    is_home_screen=False, target_control=matched.row,
                )
                executed.append(tap.get("description", "tap"))
            if decision.notes:
                self.read_results.append({
                    "name": matched.row,
                    "detail_notes": decision.notes,
                })
            return f"DETAIL {matched.row} taps={executed} notes={decision.notes!r}"
        except Exception as exc:  # noqa: BLE001
            return f"detail operate failed: {exc}"

    def _go_back(self) -> None:
        """Return to the list after a detail visit (back navigation)."""
        from gui_agent.core.schemas import BaseAction, BaseActionDecision

        try:
            self._executor.execute(
                BaseActionDecision(action=BaseAction(
                    action_type="back", description="return to list",
                )),
                app_name="", png_bytes=b"", is_home_screen=False, target_control="",
            )
        except Exception:
            pass

    @staticmethod
    def _action_coord(
        action: str,
        buttons: dict,
        plan: ScreenActionPlan | None = None,
    ) -> tuple[float, float] | None:
        """Map a free-text action to a row button coordinate by keyword.

        "删除"/"delete" -> the delete button; "勾选"/"select" -> the checkbox
        (first unlabeled control); "编辑"/"edit" -> the edit button; "打开"/"open"
        -> row open. Falls back to the checkbox when the action mentions select.

        When the pre-traversal plan says the action needs a manage/checkbox mode
        (e.g. delete via select-then-remove), a delete action uses the row's
        select (checkbox) control instead of an invisible swipe-delete layer.
        """
        lowered = action.casefold()
        manage_mode = plan is not None and "勾选" in (plan.mode_required or "")
        if "删除" in action or "delete" in lowered:
            if manage_mode:
                return buttons.get("select")
            return buttons.get("删除") or buttons.get("select")
        if "勾选" in action or "select" in lowered or "选中" in action:
            return buttons.get("select")
        if "编辑" in action or "edit" in lowered:
            return buttons.get("编辑") or buttons.get("select")
        if "打开" in action or "open" in lowered:
            return buttons.get("打开") or buttons.get("select")
        return buttons.get("select")

    def _advance(self, obs: Observation, direction: str = "down") -> bool:
        """Scroll to the next screen.

        Prefer the bound-anchored collection move; when the region is reported
        static (Android sometimes under-reports RecyclerView scrollability), fall
        back to a direct device scroll through the executor's normalized path.
        """
        candidates = collection_candidates(obs)
        region = next(
            (c for c in candidates if c.get("projection") == "cells"),
            candidates[0] if candidates else None,
        )
        if region is not None and self.bundle.move_collection is not None:
            family = "scroll_forward" if direction == "down" else "scroll_backward"
            if self.bundle.move_collection(self.platform, region["table"], family):
                return True
        # Fallback: direct scroll through the shared platform executor.
        from gui_agent.core.schemas import BaseAction, BaseActionDecision

        action = BaseAction(
            action_type="scroll", direction=direction, amount="medium",
            description="advance screen",
        )
        decision = BaseActionDecision(action=action)
        try:
            self._executor.execute(
                decision, app_name="", png_bytes=obs.png_bytes,
                is_home_screen=False, target_control="",
            )
            return True
        except Exception:
            return False


__all__ = ["ScreenDecision", "ScreenRowAction", "ScreenTableProcessor"]
