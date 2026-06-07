"""MilestoneSupervisorPolicy: two-machine milestone supervisor."""

import io
import json
import re
import time
from datetime import date
from typing import Literal, Optional

from PIL import Image, ImageStat
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from llm.structured import get_llm_token_usage, invoke_structured
from policy_expr.config import resolve_llm_config
from policy_expr.schemas import Milestone, Observation, PolicyTurn, SupervisorStep

from .helpers import _build_msgs, _format_history, _inject_knowledge, _make_llm, run_loop_check, run_planner
from .prompts import (
    DECOMPOSE_PROMPT,
    LOOP_FRAME_PROMPT,
    LOOP_SCROLL_PROMPT,
    REPLAN_PROMPT,
    STOP_CONDITION_PATCH_PROMPT,
)
from .helpers import run_checker
from .schemas import (
    _DecomposeResponse,
    _LoopFrameResult,
    _PlanResult,
    _ReplanResult,
    _SingleCheckResult,
    _StopConditionPatch,
)

MAX_RETRIES = 3
STUCK_SCREEN_WINDOW = 3
STUCK_SCREEN_SIMILARITY = 0.95
STUCK_SCREEN_FROZEN = 0.99
MAX_SCROLL_PER_MILESTONE = 3
STUCK_REPEAT_WINDOW = 3
# Blank/loading screen = the app's content body is light AND near-uniform (no
# text/list rendered yet). Measured on the de-bezeled body, not the full frame.
# This app's blank body renders ~gray 239 (not pure white), so don't gate on >250.
BLANK_BODY_MEAN_MIN = 225.0  # body must be light (rules out dark splash/dark mode)
BLANK_BODY_STD_MAX = 12.0    # body must be near-uniform (text/icons push std up)
STUCK_REPEAT_WORD_OVERLAP = 0.85
# 连续调值类（is_iterative）的卡住判据：屏幕冻结/指令重复都是正常的（picker 反复拖同一下），
# 不适用。改判「被监控值连续 N 轮未变化」=真停滞。N>1 容忍摘要框提交滞后(1 轮) + 小步未到一格。
STUCK_VALUE_STALL_WINDOW = 4


# ── Helpers ───────────────────────────────────────────────────────────


class _Timer:
    """Context manager: time a named section, accumulating into a collector dict.

    When `tokens` is given, also records the LLM input/output token delta during
    the section into tokens[name] = {"input": .., "output": ..} (same global-counter
    caveats as get_llm_call_count: concurrent calls in this window leak in).
    """

    def __init__(self, collector: dict[str, float], order: list[str], name: str,
                 tokens: "dict[str, dict[str, int]] | None" = None):
        self._c = collector
        self._o = order
        self._n = name
        self._s = 0.0
        self._tokens = tokens
        self._tok0 = (0, 0)

    def __enter__(self):
        self._s = time.perf_counter()
        if self._tokens is not None:
            self._tok0 = get_llm_token_usage()
        return self

    def __exit__(self, *a):
        d = time.perf_counter() - self._s
        if self._n not in self._c:
            self._o.append(self._n)
        self._c[self._n] = self._c.get(self._n, 0) + d
        if self._tokens is not None:
            inp, out = get_llm_token_usage()
            slot = self._tokens.setdefault(self._n, {"input": 0, "output": 0})
            slot["input"] += inp - self._tok0[0]
            slot["output"] += out - self._tok0[1]


def _is_loop(milestone: Milestone) -> bool:
    return (
        milestone.kind == "collection"
        and milestone.completion_strategy == "scroll_until_boundary"
    )


def _last_scroll_was_for(history: list[PolicyTurn], milestone_id: str) -> bool:
    return bool(
        history
        and history[-1].supervisor.milestone_id == milestone_id
        and history[-1].action_decision
        and history[-1].action_decision.action.action_type == "scroll"
        and history[-1].executed
    )


def _has_successful_scroll_for(history: list[PolicyTurn], milestone_id: str) -> bool:
    return any(
        t.supervisor.milestone_id == milestone_id
        and t.action_decision
        and t.action_decision.action.action_type in {"scroll", "drag"}
        and t.executed
        for t in history
    )


def _has_collected(history: list[PolicyTurn], milestone_id: str) -> bool:
    return any(
        t.supervisor.milestone_id == milestone_id and t.read_added_content
        for t in history
    )


def _is_blank_screen(png_bytes: bytes) -> bool:
    """Return True if the screenshot is a blank/loading screen (content not yet rendered).

    A blank/loading body is a large, LIGHT, near-UNIFORM region. We measure the
    app content body, not the full frame, because:
      - the iPhone Mirroring black bezel dilutes any whole-image ratio, and
      - this app's blank body renders ~gray 239 (not pure white), so a `>250`
        near-white count finds nothing.
    So: trim the black bezel (bbox of non-dark pixels), drop the top chrome
    (status bar / header / toolbar), then treat the body as blank when it is
    both light (mean ≥ BLANK_BODY_MEAN_MIN) and near-uniform (stddev ≤ BLANK_BODY_STD_MAX).
    Text, icons or a rendered list push the stddev up and fail the uniform test.
    """
    img = Image.open(io.BytesIO(png_bytes)).convert("L")
    # Trim the black mirroring bezel to the actual screen rectangle.
    bbox = img.point(lambda p: 255 if p > 40 else 0).getbbox()
    if bbox:
        img = img.crop(bbox)
    w, h = img.size
    if w == 0 or h == 0:
        return False
    # Measure the body below the top chrome (status bar + header + toolbar ≈ 18%)
    # and above the home indicator.
    body = img.crop((0, int(h * 0.18), w, int(h * 0.97)))
    if body.width == 0 or body.height == 0:
        return False
    stat = ImageStat.Stat(body)
    mean, std = stat.mean[0], stat.stddev[0]
    return mean >= BLANK_BODY_MEAN_MIN and std <= BLANK_BODY_STD_MAX


def _default_read_instruction(milestone: Milestone) -> str:
    return (
        f"提取当前屏幕中与「{milestone.name}」相关的所有可见内容，"
        "保留名称/标题、时间/位置、目标相关数值、状态、类别等字段；如果是列表，逐条提取。"
    )


def _ctx(milestone: Milestone, read_instruction: Optional[str], collection_scope=None) -> dict:
    allow_read = milestone.kind in {"collection", "verification"}
    return {
        "read_instruction": read_instruction,
        "allow_read": bool(read_instruction and allow_read),
        "milestone_id": milestone.id,
        "milestone_kind": milestone.kind,
        "completion_strategy": milestone.completion_strategy,
        "collection_scope": collection_scope,
    }


def _is_home_identity(page_identity: str) -> bool:
    """Derive 'on the iOS home screen' from the checker's free-text page_identity.

    Home-screen icon labels are not tappable (only the icon glyph launches the
    app), so the executor must skip OCR text-snap there. The checker reliably
    writes '主屏' for springboard; also accept the English variants.
    """
    pid = (page_identity or "").lower()
    return "主屏" in (page_identity or "") or "home screen" in pid or "springboard" in pid


def _png_sim(png1: bytes, png2: bytes, size: int = 64) -> float:
    img1 = Image.open(io.BytesIO(png1)).convert("L").resize((size, size))
    img2 = Image.open(io.BytesIO(png2)).convert("L").resize((size, size))
    total = sum(abs(int(a) - int(b)) for a, b in zip(img1.getdata(), img2.getdata()))
    return 1.0 - total / (255 * size * size)


# ── Main class ────────────────────────────────────────────────────────


class MilestoneSupervisorPolicy:
    """Two-machine milestone supervisor: single-step and loop run independently."""

    name = "milestone"

    def __init__(self) -> None:
        self._global_constraints: list[str] = []
        self._milestones: dict[str, Milestone] = {}
        self._order: list[str] = []
        self._current_id: Optional[str] = None
        self._recent_screenshots: list[bytes] = []
        self._scroll_counts: dict[str, int] = {}
        self.task_type: Literal["action", "analysis"] = "action"
        self._app_knowledge: Optional[str] = None
        self._elements_knowledge: Optional[str] = None
        self._app_name: str = ""
        self._last_page_identity: dict[str, str] = {}
        self._last_check_summary: dict[str, str] = {}
        # 连续调值类的进展追踪：每 milestone 一个滑动窗口，存最近若干轮 checker 读到的「当前值」。
        self._progress_values: dict[str, list[str]] = {}
        self._last_check: Optional[_SingleCheckResult] = None
        self._milestone_done_checks: dict[str, "_SingleCheckResult"] = {}  # milestone_id → done check
        self._last_plan: Optional[_PlanResult] = None
        self._last_replan: Optional[_ReplanResult] = None
        self._timings: dict[str, float] = {}
        self._timings_order: list[str] = []
        self._token_usage: dict[str, dict[str, int]] = {}   # per-module {input, output}

    def set_app_knowledge(self, text: str, app_name: str = "", elements: str = "") -> None:
        self._app_knowledge = text
        self._elements_knowledge = elements or None
        if app_name:
            self._app_name = app_name

    def step(self, observation: Observation, goal: str, history: list[PolicyTurn]) -> SupervisorStep:
        self._timings.clear()
        self._timings_order.clear()
        self._token_usage.clear()

        if not self._order:
            with _Timer(self._timings, self._timings_order, "decompose", self._token_usage):
                self._decompose(goal, observation)

        if self._current_id is None:
            return self._terminal_step()

        milestone = self._milestones[self._current_id]
        if _is_loop(milestone):
            result = self._run_loop_turn(milestone, observation, history)
        else:
            result = self._run_single_turn(milestone, observation, history)

        return result

    # ── Single-step machine ───────────────────────────────────────────

    def _run_single_turn(
        self,
        milestone: Milestone,
        observation: Observation,
        history: list[PolicyTurn],
    ) -> SupervisorStep:
        if _is_blank_screen(observation.png_bytes):
            print("  [BlankScreen] 检测到白屏，页面加载中，等待下一帧...")
            return SupervisorStep(
                should_act=False,
                instruction=None,
                stop=False,
                goal_completed=False,
                is_loading=True,
                summary="页面加载中（白屏），等待...",
                **_ctx(milestone, None),
            )

        if history and history[-1].action_decision:
            if history[-1].action_decision.action.action_type == "type":
                self._recent_screenshots.clear()

        # Off-target last action (post-action targeting verify said the tap
        # missed): skip the checker — the milestone obviously isn't done — and
        # route straight into replan. Catches "screen changed but to the wrong
        # element" (e.g. 搜索框 tap hit 转账 tab), which SimStuck can't.
        last_tv = history[-1].target_verify if history else None
        if last_tv is not None and not last_tv.on_target:
            print(f"  [OffTarget] 上一步误中「{last_tv.actual_element}」，跳过 checker 直接 replan")
            stuck = _SingleCheckResult(
                status="stuck",
                reason=f"上一步动作落点误中「{last_tv.actual_element}」，未点到目标",
                stuck_reason=f"动作 off-target：误中{last_tv.actual_element}",
                summary="",
            )
            return self._handle_stuck(milestone, stuck, None, observation, history)

        # Ineffective last tap: target_verify said on_target (hit the intended
        # element) yet settle saw zero screen change across all polls → the tap
        # did nothing (re-tapped an already-active tab / inert element). Skip the
        # checker and replan now, instead of waiting ~3 frames for SimStuck to
        # fill. Complements off_target above: that owns wrong-element (on_target
        # False); this owns right-element-but-no-effect. Gated on tap so gestures
        # (which legitimately may not move much) never trigger it.
        last_turn = history[-1] if history else None
        if (
            last_turn is not None
            and getattr(last_turn, "no_effect", False)
            and last_turn.action_decision is not None
            and last_turn.action_decision.action.action_type in ("tap", "click")
            and (last_tv is None or last_tv.on_target)
        ):
            tapped = last_turn.action_decision.action.description or "目标元素"
            print(f"  [NoEffect] 上一步点击「{tapped}」落点正确但屏幕零变化（已在该态/元素无效），跳过 checker 直接 replan")
            stuck = _SingleCheckResult(
                status="stuck",
                reason=f"上一步点击「{tapped}」落点正确但屏幕无变化，该操作对当前页面无效",
                stuck_reason=f"动作无效果：点击「{tapped}」屏幕零变化，应换路径",
                summary="",
            )
            return self._handle_stuck(milestone, stuck, None, observation, history)

        prev_page_id = self._last_page_identity.get(milestone.id, "")

        with _Timer(self._timings, self._timings_order, "checker", self._token_usage):
            check = self._single_check(milestone, observation, history)
        self._last_check = check
        print(f"  [SingleCheck] {check.status}: {check.reason}")

        if check.loading:
            print("  [Loading] 检测到加载状态，等待下一帧...")
            return SupervisorStep(
                should_act=False, instruction=None, stop=False,
                goal_completed=False, is_loading=True, summary="页面加载中，等待...",
                **_ctx(milestone, None),
            )

        current_page_id = check.page_identity or ""
        self._last_page_identity[milestone.id] = current_page_id

        if check.status == "done":
            return self._advance(milestone, observation, history)

        # 连续调值类（picker/步进器收敛）：SimStuck/RepStuck 不适用（屏幕冻结、反复拖同一下
        # 都正常）。改用「被监控值多轮不变」判停滞；未停滞就继续规划，不会因单轮无进展判死路。
        if milestone.is_iterative:
            self._last_check_summary[milestone.id] = check.summary
            stall = self._check_value_stall(milestone, check)
            if stall is not None:
                return self._handle_stuck(
                    milestone, stall, check.read_instruction, observation, history,
                    page_changed=False,
                    prev_page_id=prev_page_id, current_page_id=current_page_id,
                )
            return self._plan_single(milestone, check, observation, history)

        sim_stuck = self._check_screen_similarity(observation)

        prev_check_summary = self._last_check_summary.get(milestone.id, "")
        prev_action = history[-1].action_decision.action if history and history[-1].action_decision else None
        was_picker_drag = (
            prev_action is not None
            and prev_action.action_type == "drag"
            and (prev_action.target_area or "").startswith("picker_")
        )
        if sim_stuck is not None and sim_stuck.frozen and was_picker_drag and prev_check_summary and prev_check_summary != check.summary:
            print(f"  [SimStuck] 已抑制：picker 进展（frozen+摘要变化）")
            sim_stuck = None
            self._recent_screenshots.clear()
        self._last_check_summary[milestone.id] = check.summary

        rep_stuck = self._check_instruction_repetition(history, milestone.id) if not sim_stuck else None
        if sim_stuck or rep_stuck:
            stuck = sim_stuck or rep_stuck
            assert stuck is not None
            print(f"  [Stuck] {stuck.status}: {stuck.reason}")
            page_changed = sim_stuck is None
            return self._handle_stuck(
                milestone, stuck, check.read_instruction, observation, history,
                page_changed=page_changed,
                prev_page_id=prev_page_id,
                current_page_id=current_page_id,
            )
        return self._plan_single(milestone, check, observation, history)

    def _plan_single(
        self,
        milestone: Milestone,
        check: _SingleCheckResult,
        observation: Observation,
        history: list[PolicyTurn],
    ) -> SupervisorStep:
        with _Timer(self._timings, self._timings_order, "planner", self._token_usage):
            plan = self._invoke_planner(milestone, check, observation, history)
        if self._is_sequence(plan.instruction):
            print("  [Planner] 多步序列，重试...")
            with _Timer(self._timings, self._timings_order, "planner", self._token_usage):
                plan = self._invoke_planner(
                    milestone, check, observation, history,
                    extra="你刚才输出了多个步骤，请只返回当前屏幕上马上要做的一个操作。",
                )
        # 连续调值类豁免「指令重复=失败」升级：对 picker 而言重复"向上拖月份列"直到到位本就
        # 是正确做法，单步式的重复检测会误把它判成撞墙→死路。其停滞由 _check_value_stall 兜。
        if not milestone.is_iterative and self._is_repeated_instruction(plan.instruction, milestone.id, history):
            print("  [Planner] 指令重复已失败操作，重试...")
            with _Timer(self._timings, self._timings_order, "planner", self._token_usage):
                plan = self._invoke_planner(
                    milestone, check, observation, history,
                    extra=(
                        "你刚才的指令与之前失败的操作相同。"
                        "请仔细查看截图，找一个不同的 UI 元素或操作路径。"
                    ),
                )
            if self._is_repeated_instruction(plan.instruction, milestone.id, history):
                print("  [Planner] 重试仍重复，升级为 stuck 处理")
                stuck_check = _SingleCheckResult(
                    status="stuck",
                    reason=f"planner 无法找到与之前不同的操作路径，已尝试指令均导致错误",
                    stuck_reason="planner 陷入重复，无法生成新操作",
                    summary=check.summary,
                )
                return self._handle_stuck(milestone, stuck_check, check.read_instruction, observation, history)
        print(f"  [Planner] {plan.instruction}")
        # 结构化 drag 距离：当 planner 给出本列的当前值/目标值时，按差几格算出 drag_steps，
        # 让 action policy 据此放大拖动幅度（差 20 格用 large 粗调、接近后自动收回 small 精调）。
        # 这条结构化通路绕开「从 instruction 文本正则抠数字」的脆弱性——指令只写了目标值
        # （如「直到显示21日」缺当前值）时，正则抠不到距离会退化成一格一格挪、跑满轮数也到不了。
        drag_steps = self._picker_drag_steps(plan)
        if drag_steps is not None and plan.drag_column:
            print(f"  [Planner] hints: direction={plan.direction} column={plan.drag_column} steps={drag_steps}")
        elif plan.direction or plan.drag_column:
            print(f"  [Planner] hints: direction={plan.direction} column={plan.drag_column}")
        if plan.direction in ("increase", "decrease") and plan.drag_column:
            self._fix_picker_direction(plan)
        self._last_plan = plan
        milestone.status = "running"
        return SupervisorStep(
            should_act=bool(plan.instruction),
            instruction=plan.instruction or None,
            stop=False,
            goal_completed=False,
            summary=plan.summary,
            direction=plan.direction,
            drag_column=plan.drag_column,
            drag_steps=drag_steps,
            is_home_screen=_is_home_identity(check.page_identity),
            **_ctx(milestone, check.read_instruction),
        )

    # ── Loop machine ──────────────────────────────────────────────────

    def _run_loop_turn(
        self,
        milestone: Milestone,
        observation: Observation,
        history: list[PolicyTurn],
    ) -> SupervisorStep:
        self._scroll_counts[milestone.id] = self._scroll_counts.get(milestone.id, 0) + 1
        scroll_count = self._scroll_counts[milestone.id]

        if milestone.scroll_budget > 0:
            budget = milestone.scroll_budget
        elif milestone.observable_boundary:
            budget = 10
        else:
            budget = MAX_SCROLL_PER_MILESTONE
        if scroll_count > budget:
            print(f"  [Loop] 滚动预算耗尽（{scroll_count}/{budget}，observable={milestone.observable_boundary}）→ 结束收集")
            if not _has_successful_scroll_for(history, milestone.id):
                stuck = _SingleCheckResult(
                    status="stuck",
                    reason="滚动预算耗尽，但尚未观测到任何成功执行的纵向滚动",
                    stuck_reason="无法区分页面边界与无效滚动，且缺少有效滚动证据",
                    issues=["没有成功滚动记录"],
                    summary="滚动未取得可验证进展",
                )
                read_inst = None if self.task_type == "action" else _default_read_instruction(milestone)
                return self._handle_stuck(milestone, stuck, read_inst, observation, history)
            return self._advance(milestone, observation, history)

        sim_stuck = self._check_screen_similarity(observation)
        last_read_added = bool(
            history
            and history[-1].supervisor.milestone_id == milestone.id
            and history[-1].read_added_content
        )
        if sim_stuck:
            if sim_stuck.frozen:
                if not _has_successful_scroll_for(history, milestone.id):
                    print("  [Loop] 屏幕冻结且无成功滚动证据 → 判为无效滚动，触发重规划")
                    read_inst = None if self.task_type == "action" else _default_read_instruction(milestone)
                    return self._handle_stuck(milestone, sim_stuck, read_inst, observation, history)
                print("  [Loop] 屏幕冻结（≥99%），即使 reader 返回新内容也结束收集")
                return self._advance(milestone, observation, history)
            if not last_read_added:
                if not _has_successful_scroll_for(history, milestone.id):
                    print("  [Loop] 截图连续无变化且无成功滚动证据 → 判为无效滚动，触发重规划")
                    read_inst = None if self.task_type == "action" else _default_read_instruction(milestone)
                    return self._handle_stuck(milestone, sim_stuck, read_inst, observation, history)
                print("  [Loop] 截图连续无变化且无新增内容 → 判为边界，结束收集")
                return self._advance(milestone, observation, history)
            print("  [Loop] 截图相似但上一轮读到了新内容，继续收集")

        with _Timer(self._timings, self._timings_order, "loop_check", self._token_usage):
            frame = self._loop_check(milestone, observation, history)
        self._last_check = None  # loop milestones use _LoopFrameResult, not _SingleCheckResult
        print(f"  [LoopFrame] boundary={frame.boundary_reached}, should_stop={frame.should_stop}")

        # 采集启动阶段（首次成功滚动之前）拦截 loading 帧：刚应用筛选/进入列表时，页面常还在
        # 「加载中」、列表体仍显示旧内容（如范围外的更晚日期）。若读入会污染 content_notes 并把
        # stitch 基线钉在旧帧上，导致后续真实内容拼接错位、漏采中段（实测 20260607_105731：
        # 读了加载中的旧帧 → 漏掉 5/24-28、混入 5/29）。返回 is_loading 等待帧，由 runner 短路
        # 跳过本帧（不读、不喂 stitch、不计轮数）。一旦开始滚动，内容已确认渲染，不再判 loading。
        if frame.loading and not _has_successful_scroll_for(history, milestone.id):
            print("  [Loop] 采集启动帧仍在加载中 → 等待重渲染，不读取本帧")
            self._scroll_counts[milestone.id] -= 1  # 加载等待不计入滚动预算
            return SupervisorStep(
                should_act=False, stop=False, goal_completed=False, is_loading=True,
                summary="采集页加载中，等待...",
                **_ctx(milestone, None),
            )

        if frame.should_stop:
            print(f"  [Loop] 停止条件触发：{frame.stop_reason}")
        if self.task_type == "action":
            read_inst = None
        else:
            read_inst = frame.read_instruction or _default_read_instruction(milestone)

        if frame.should_stop:
            if _has_collected(history, milestone.id):
                print("  [Loop] 已触发停止条件且有采集内容 → 结束收集")
                final_read = _ctx(milestone, read_inst, frame.collection_scope)
                if milestone.scroll_stop_condition:
                    final_read["collection_summary"] = (
                        f"停止条件「{milestone.scroll_stop_condition}」已触发"
                        f"（{frame.stop_reason}）"
                    )
                return self._advance(
                    milestone, observation, history,
                    final_read=final_read,
                )
            # 停止条件触发、但本采集子目标尚未采到任何内容。
            if not _has_successful_scroll_for(history, milestone.id):
                # ⚠️ 还没滚动过就报"停止/到底"不可信：刚进入采集子目标的首帧，列表常一屏可见、
                # 下方却还有内容（这正是 force_complete 误判、零采集即完成的根源——筛选结果一屏
                # 显示完就被判 done，实则可滚）。强制先滚一次采集、用真实滚动验证边界，绝不在
                # 零滚动+零采集时结束收集。落到下方滚动规划块。
                print("  [Loop] 停止条件触发但尚未滚动过且零采集 → 先强制滚动一次验证边界，不判完成")
            else:
                stuck = _SingleCheckResult(
                    status="stuck",
                    reason=f"停止条件已触发但尚未采集到目标内容：{frame.stop_reason}",
                    stuck_reason="停止条件触发且没有可用采集结果",
                    summary=frame.summary,
                )
                return self._handle_stuck(milestone, stuck, read_inst, observation, history)

        if frame.boundary_reached and _last_scroll_was_for(history, milestone.id):
            print("  [Loop] 确认列表边界 → 结束收集")
            return self._advance(milestone, observation, history)

        milestone.status = "running"
        loop_summary_prefix = "继续滚动查找目标" if self.task_type == "action" else "继续滚动收集内容"
        if _last_scroll_was_for(history, milestone.id):
            return SupervisorStep(
                should_act=True,
                instruction="继续滚动",
                preformed_action=history[-1].action_decision,
                stop=False,
                goal_completed=False,
                summary=f"{loop_summary_prefix}。{frame.summary}",
                read_instruction=read_inst,
                allow_read=bool(read_inst),
                milestone_id=milestone.id,
                milestone_kind=milestone.kind,
                completion_strategy=milestone.completion_strategy,
                collection_scope=frame.collection_scope,
            )

        with _Timer(self._timings, self._timings_order, "loop_scroll", self._token_usage):
            plan = self._invoke_loop_scroll(milestone, frame, observation)
        print(f"  [LoopScroll] {plan.instruction}")
        return SupervisorStep(
            should_act=True,
            instruction=plan.instruction,
            stop=False,
            goal_completed=False,
            summary=plan.summary,
            read_instruction=read_inst,
            allow_read=bool(read_inst),
            milestone_id=milestone.id,
            milestone_kind=milestone.kind,
            completion_strategy=milestone.completion_strategy,
            collection_scope=frame.collection_scope,
        )

    # ── Shared: advance, stuck, terminal ─────────────────────────────

    def _advance(
        self,
        milestone: Milestone,
        observation: Observation,
        history: list[PolicyTurn],
        final_read: Optional[dict] = None,
    ) -> SupervisorStep:
        done_name = milestone.name
        milestone.status = "done"
        self._current_id = self._next_milestone()
        self._recent_screenshots.clear()
        print(f"  子目标「{done_name}」已完成")

        pre_existing = not any(
            t.executed for t in history
            if t.supervisor.milestone_id == milestone.id
        )
        if pre_existing:
            print(f"  [PreExisting] 子目标「{done_name}」未执行任何动作即判完成，目标状态在会话前已存在")

        if self._current_id is None:
            # final_read(_ctx) 已含 milestone_id/kind/completion_strategy；显式再传会撞车
            # （TypeError: got multiple values for 'milestone_id'）。合并为一个 ctx：无
            # final_read 时只带这三个 milestone 字段，有则其同名键(同值)覆盖、并补 read_* 等。
            ctx = {
                "milestone_id": milestone.id,
                "milestone_kind": milestone.kind,
                "completion_strategy": milestone.completion_strategy,
                **(final_read or {}),
            }
            return SupervisorStep(
                should_act=False, stop=True, stop_reason="所有子目标已完成",
                goal_completed=True, pre_existing=pre_existing,
                summary=f"子目标「{done_name}」已完成，任务全部完成。",
                **ctx,
            )

        next_ms = self._milestones[self._current_id]
        print(f"  开始执行「{next_ms.name}」")

        if final_read:
            return SupervisorStep(
                should_act=False, stop=False, goal_completed=False,
                summary=f"子目标「{done_name}」已完成，下一子目标「{next_ms.name}」待执行。",
                **final_read,
            )

        if _is_loop(next_ms):
            return self._run_loop_turn(next_ms, observation, history)

        # Freshly entered navigation milestone: it depends on the just-completed
        # one, so it is in_progress by construction. Skip its initial done-check
        # and plan the first nav action directly — this drops the 2nd checker
        # call on advance turns (the user-visible "2 轮 checker" latency). The
        # frame is known-loaded here (we only reach _advance after a done check
        # on this same frame). A residual already-done state is caught by the
        # next turn's normal check; re-tapping a nav target is idempotent so the
        # extra action is harmless. Other kinds keep the check — re-running an
        # action milestone could double-execute (re-send/re-submit), and
        # collection/verification need the checker's read_instruction.
        if next_ms.kind == "navigation":
            print("  [SkipCheck] 新进入导航子目标，跳过首次验收，直接规划")
            # Save the done check for the completed milestone before overwriting _last_check.
            if self._last_check is not None:
                self._milestone_done_checks[milestone.id] = self._last_check
            synthetic = _SingleCheckResult(
                status="in_progress",
                reason=f"刚进入子目标「{next_ms.name}」，默认未完成",
                summary="",
            )
            self._last_check = synthetic
            self._last_page_identity[next_ms.id] = ""
            return self._plan_single(next_ms, synthetic, observation, history)

        return self._run_single_turn(next_ms, observation, history)

    def _handle_stuck(
        self,
        milestone: Milestone,
        check: _SingleCheckResult,
        read_inst: Optional[str],
        observation: Observation,
        history: list[PolicyTurn],
        page_changed: bool = True,
        prev_page_id: str = "",
        current_page_id: str = "",
    ) -> SupervisorStep:
        self._recent_screenshots.clear()
        skip_retry = False
        if history and history[-1].supervisor and history[-1].supervisor.milestone_id == milestone.id:
            if history[-1].supervisor.instruction and not history[-1].executed:
                print(f"  [Replan] 上一轮指令未执行，不计入重试次数")
                skip_retry = True
            elif page_changed:
                truly_new_page = bool(prev_page_id and current_page_id and prev_page_id != current_page_id)
                if truly_new_page:
                    print(f"  [Replan] 页面已跳转（{prev_page_id} → {current_page_id}），不计入重试次数")
                    skip_retry = True
                    before = len(self._global_constraints)
                    self._global_constraints = [
                        c for c in self._global_constraints
                        if not (c.startswith("指令「") and "禁止重复此指令" in c)
                    ]
                    cleared = before - len(self._global_constraints)
                    if cleared:
                        print(f"  [Replan] 清除 {cleared} 条旧页面操作约束")
                else:
                    print(f"  [Replan] 屏幕变化但页面未变（{current_page_id or '未知'}），计入重试次数")
        if not skip_retry:
            milestone.retry_count += 1

        self._record_failure_constraint(milestone, check, history)

        if milestone.retry_count >= MAX_RETRIES:
            fallback = self._try_filter_fallback(milestone, can_degrade=True, read_inst=read_inst)
            if fallback:
                return fallback
            return self._fail(milestone, check, read_inst)

        print(f"  [Replan] 第 {milestone.retry_count} 次重试...")
        # 连续调值类停滞时的纠偏方向：继续用拖动（picker 本就只能拖），换一种连续策略——
        # 加大拖动幅度 / 换调整的列 / 调整顺序（如先把"日"调到合法范围再调"月"，破非法日期回弹死锁）。
        # 绝不退化成点击 picker 滚轮数值（iOS 滚轮不认点击），也不要判"组件不支持拖拽"。
        iter_extra = (
            "⚠️ 当前子目标是连续调值类（靠滚轮/步进器反复拖动逼近目标值）。停滞≠不支持拖拽。"
            "必须继续用拖动，换一种连续策略：加大拖动幅度、或换要调整的列、或调整顺序"
            "（如把'日'先调到目标月份的合法范围内、再调'月'，以破解非法日期被回弹的死锁）。"
            "禁止改用点击 picker 滚轮上的具体数值（iOS 滚轮不响应点击），禁止判定'组件不支持滚动/拖拽'。"
        ) if milestone.is_iterative else ""
        with _Timer(self._timings, self._timings_order, "replanner", self._token_usage):
            replan = self._invoke_replanner(milestone, check, observation, history, extra=iter_extra)
        self._last_replan = replan
        print(f"  [Replan] 诊断={replan.diagnosis}, 策略={replan.strategy}")

        # Persist diagnosis as a global constraint so the regular Planner (not just
        # the next Replan prompt) avoids re-entering the same dead-end path.
        if replan.diagnosis:
            dead_end_constraint = f"⚠️ 已诊断死路-禁止重走：{replan.diagnosis}"
            if dead_end_constraint not in self._global_constraints:
                self._global_constraints.append(dead_end_constraint)

        if replan.strategy == "force_complete":
            print(f"  [Replan] replanner 判定验收条件已满足，强制完成")
            return self._advance(milestone, observation, history)

        if replan.strategy == "escalate_human":
            fallback = self._try_filter_fallback(
                milestone, can_degrade=replan.can_degrade_to_collection, read_inst=read_inst,
            )
            if fallback:
                return fallback
            milestone.status = "failed"
            self._current_id = self._next_milestone()
            return SupervisorStep(
                should_act=False,
                stop=self._current_id is None,
                stop_reason=replan.escalation_message or "升级人工介入",
                goal_completed=False,
                summary=replan.diagnosis,
                **_ctx(milestone, read_inst),
            )

        milestone.status = "running"
        return SupervisorStep(
            should_act=bool(replan.instruction),
            instruction=replan.instruction or None,
            stop=False,
            goal_completed=False,
            summary=f"子目标「{milestone.name}」卡住，第 {milestone.retry_count} 次重试。{replan.diagnosis}",
            is_home_screen=_is_home_identity(self._last_check.page_identity) if self._last_check else False,
            **_ctx(milestone, read_inst),
        )

    def _fail(self, milestone: Milestone, check: _SingleCheckResult, read_inst: Optional[str]) -> SupervisorStep:
        milestone.status = "failed"
        self._current_id = self._next_milestone()
        print(f"  子目标「{milestone.name}」失败")
        if self._current_id is None:
            return SupervisorStep(
                should_act=False, stop=True,
                stop_reason=f"子目标「{milestone.name}」重试 {MAX_RETRIES} 次后失败",
                goal_completed=False, summary=check.reason,
                **_ctx(milestone, read_inst),
            )
        return SupervisorStep(
            should_act=False, stop=False, goal_completed=False,
            summary=f"子目标「{milestone.name}」失败，跳过继续下一个。",
            **_ctx(self._milestones[self._current_id], read_inst),
        )

    def _terminal_step(self) -> SupervisorStep:
        failed = [m for m in self._milestones.values() if m.status == "failed"]
        pending = [m for m in self._milestones.values() if m.status == "pending"]
        if failed or pending:
            return SupervisorStep(
                should_act=False, stop=True, goal_completed=False,
                stop_reason=f"无可执行子目标；失败：{'、'.join(m.name for m in failed) or '无'}；未完成：{'、'.join(m.name for m in pending) or '无'}",
                summary="任务未完成，存在失败或依赖未满足的子目标。",
            )
        return SupervisorStep(
            should_act=False, stop=True, stop_reason="所有子目标已完成",
            goal_completed=True, summary="任务完成",
        )

    def _try_filter_fallback(
        self,
        milestone: Milestone,
        can_degrade: bool,
        read_inst: Optional[str],
    ) -> Optional[SupervisorStep]:
        if milestone.kind != "filter" or not can_degrade:
            return None
        dependent = next(
            (self._milestones[mid] for mid in self._order
             if self._milestones[mid].status == "pending"
             and milestone.id in self._milestones[mid].depends_on
             and self._milestones[mid].kind == "collection"),
            None,
        )
        if dependent is None:
            return None
        if _is_loop(dependent):
            filter_intent = milestone.success_condition
            dependent.scroll_stop_condition = (
                f"当可见内容不再满足筛选条件「{filter_intent}」时停止滚动"
            )
            dependent.observable_boundary = False
            dependent.scroll_budget = 15
        milestone.status = "done"
        self._current_id = dependent.id
        self._recent_screenshots.clear()
        msg = (
            f"子目标「{milestone.name}」无法精确筛选，已降级为在「{dependent.name}」阶段收集并过滤。"
        )
        if msg not in self._global_constraints:
            self._global_constraints.append(msg)
        print(f"  [Fallback] {msg}")
        return SupervisorStep(
            should_act=False, stop=False, goal_completed=False, summary=msg,
            **_ctx(dependent, read_inst),
        )

    # ── LLM invocations ───────────────────────────────────────────────

    def _record_failure_constraint(
        self,
        milestone: Milestone,
        check: _SingleCheckResult,
        history: list[PolicyTurn],
    ) -> None:
        reason = check.stuck_reason or check.reason
        if "planner 陷入重复" in reason:
            return
        # 连续调值类不记「禁止重复此指令」：会把唯一可靠的拖动操作拉黑，逼系统退化成点击 picker
        # （iOS 滚轮不认点击）。停滞时的纠偏靠 replan 换连续策略（换列/加大步长/调顺序），而非禁操作。
        if milestone.is_iterative:
            return
        last_action = next(
            (t for t in reversed(history)
             if t.supervisor and t.supervisor.milestone_id == milestone.id
             and t.supervisor.instruction and t.executed),
            None,
        )
        if not last_action:
            return
        instruction = last_action.supervisor.instruction
        constraint = f"指令「{instruction}」导致错误：{reason}，禁止重复此指令"
        if constraint not in self._global_constraints:
            self._global_constraints.append(constraint)
            print(f"  [Constraint] {constraint}")

    def _single_check(
        self,
        milestone: Milestone,
        observation: Observation,
        history: list[PolicyTurn],
        extra: str = "",
    ) -> _SingleCheckResult:
        app_name = self._app_name
        if not app_name:
            for t in reversed(history):
                if t.supervisor and t.supervisor.app_name:
                    app_name = t.supervisor.app_name
                    break
        return run_checker(
            milestone, observation, history,
            app_name=app_name,
            task_type=self.task_type,
            constraints=self._global_constraints,
            extra=extra,
        )

    def _loop_check(
        self,
        milestone: Milestone,
        observation: Observation,
        history: list[PolicyTurn],
    ) -> _LoopFrameResult:
        return run_loop_check(
            milestone, observation, history, constraints=self._global_constraints,
        )

    def _invoke_planner(
        self,
        milestone: Milestone,
        check: _SingleCheckResult,
        observation: Observation,
        history: list[PolicyTurn],
        extra: str = "",
    ) -> _PlanResult:
        return run_planner(
            milestone, check, observation, history,
            constraints=self._global_constraints,
            extra=extra,
            app_knowledge=self._app_knowledge,
            elements_knowledge=self._elements_knowledge,
        )

    def _invoke_loop_scroll(
        self,
        milestone: Milestone,
        frame: _LoopFrameResult,
        observation: Observation,
    ) -> _PlanResult:
        prompt = LOOP_SCROLL_PROMPT.format(
            milestone_name=milestone.name,
            milestone_desc=milestone.description,
            constraints=json.dumps(self._global_constraints, ensure_ascii=False),
            frame_summary=frame.summary,
        )
        return invoke_structured(self._llm(), self._msgs(prompt, observation), _PlanResult)

    def _invoke_replanner(
        self,
        milestone: Milestone,
        check: _SingleCheckResult,
        observation: Observation,
        history: list[PolicyTurn],
        extra: str = "",
    ) -> _ReplanResult:
        tried = sorted({
            t.supervisor.instruction
            for t in history
            if t.supervisor
            and t.supervisor.instruction
            and t.supervisor.milestone_id == milestone.id
        })
        tried_text = "\n".join(f"  - 「{i}」" for i in tried) if tried else "  （无）"
        done_lines = [
            f"  - [{m.id}] {m.name}（已完成，不要退回到该状态）"
            for m in self._milestones.values()
            if m.status == "done" and m.id != milestone.id
        ]
        done_context = "\n".join(done_lines) if done_lines else "  （无）"
        prompt = REPLAN_PROMPT.format(
            milestone_name=milestone.name,
            milestone_desc=milestone.description,
            success_condition=milestone.success_condition,
            stuck_reason=check.stuck_reason or check.reason,
            issues=json.dumps(check.issues, ensure_ascii=False),
            retry_count=milestone.retry_count,
            constraints=json.dumps(self._global_constraints, ensure_ascii=False),
            failure_hints=json.dumps(milestone.failure_hints, ensure_ascii=False),
            completed_milestones=done_context,
            history_text=_format_history(history),
            tried_instructions=tried_text,
        )
        if extra:
            prompt += f"\n\n## 输出修正要求\n{extra}"
        msgs = self._msgs(prompt, observation)
        _inject_knowledge(msgs, self._app_knowledge, self._elements_knowledge)
        result = invoke_structured(self._llm(), msgs, _ReplanResult)
        if self._is_sequence(result.instruction):
            print("  [Replan] 多步序列，重试...")
            result = self._invoke_replanner(
                milestone, check, observation, history,
                extra="你刚才输出了多个步骤，请只返回一个原子操作。",
            )
        return result

    # ── Stuck detection ───────────────────────────────────────────────

    def _check_screen_similarity(self, observation: Observation) -> Optional[_SingleCheckResult]:
        self._recent_screenshots.append(observation.png_bytes)
        if len(self._recent_screenshots) > STUCK_SCREEN_WINDOW:
            self._recent_screenshots.pop(0)
        if len(self._recent_screenshots) < STUCK_SCREEN_WINDOW:
            return None

        current = self._recent_screenshots[-1]
        sims = [_png_sim(current, p) for p in self._recent_screenshots[:-1]]
        max_sim = max(sims)
        if all(s >= STUCK_SCREEN_SIMILARITY for s in sims):
            sim_str = ", ".join(f"{s:.2%}" for s in sims)
            frozen = max_sim >= STUCK_SCREEN_FROZEN
            if frozen:
                print(f"  [SimStuck] {sim_str} → 屏幕冻结（≥{STUCK_SCREEN_FROZEN:.0%}）")
            else:
                print(f"  [SimStuck] {sim_str} → 截图连续无变化")
            return _SingleCheckResult(
                status="stuck",
                reason=f"连续 {STUCK_SCREEN_WINDOW} 帧截图相似度 [{sim_str}]，屏幕无实质变化",
                stuck_reason="连续帧高度相似，上一步操作未生效",
                issues=["屏幕像素变化低于阈值"],
                summary="屏幕连续无变化",
                frozen=frozen,
            )
        sim_2back = _png_sim(self._recent_screenshots[-1], self._recent_screenshots[-3])
        sim_adj = _png_sim(self._recent_screenshots[-1], self._recent_screenshots[-2])
        if sim_2back >= STUCK_SCREEN_SIMILARITY and sim_adj < STUCK_SCREEN_SIMILARITY:
            print(f"  [SimStuck] 2back={sim_2back:.2%}, adj={sim_adj:.2%} → AB 循环")
            return _SingleCheckResult(
                status="stuck",
                reason=f"截图在两种状态间交替（2帧前 {sim_2back:.2%}，相邻帧 {sim_adj:.2%}）",
                stuck_reason="屏幕在两种状态间振荡，操作陷入 AB 交替循环",
                issues=["截图在两个视觉状态间交替出现"],
                summary="屏幕在两种状态间振荡",
            )
        return None

    def _check_instruction_repetition(
        self,
        history: list[PolicyTurn],
        milestone_id: str,
    ) -> Optional[_SingleCheckResult]:
        recent_insts = [
            t.supervisor.instruction
            for t in history[-STUCK_REPEAT_WINDOW:]
            if t.supervisor
            and t.supervisor.instruction
            and t.supervisor.milestone_id == milestone_id
        ]
        if len(recent_insts) < STUCK_REPEAT_WINDOW:
            return None
        base_words = set(recent_insts[-1].split())
        sims = [
            len(base_words & set(inst.split())) / max(len(base_words), len(set(inst.split())), 1)
            for inst in recent_insts[:-1]
        ]
        if all(s >= STUCK_REPEAT_WORD_OVERLAP for s in sims):
            sim_str = ", ".join(f"{s:.2%}" for s in sims)
            print(f"  [RepStuck] {sim_str} → 指令连续重复")
            return _SingleCheckResult(
                status="stuck",
                reason=f"连续 {STUCK_REPEAT_WINDOW} 步指令词语重叠 [{sim_str}]，操作策略未变化",
                stuck_reason="连续相似指令，重复操作未生效",
                issues=["supervisor 指令持续重复"],
                summary="操作陷入重复循环",
            )
        return None

    @staticmethod
    def _extract_progress_value(check: _SingleCheckResult) -> str:
        """从 checker 输出抽出「当前值」作为连续操作的进展度量。

        连续调值 section 要求 checker 在 missing_evidence 写「当前值=...」；优先取它，
        取不到则退回 summary（值变化时 summary 通常也变）。返回归一化字符串供逐轮比对。
        """
        for ev in check.missing_evidence or []:
            m = re.search(r"当前值\s*[=:：]\s*(.+)", ev.strip())
            if m:
                return re.sub(r"\s+", "", m.group(1))
        return re.sub(r"\s+", "", check.summary or "")

    def _check_value_stall(
        self, milestone: Milestone, check: _SingleCheckResult,
    ) -> Optional[_SingleCheckResult]:
        """连续调值类专用卡住判据：被监控值连续 STUCK_VALUE_STALL_WINDOW 轮不变 = 真停滞。

        替代 SimStuck/RepStuck——对 picker 而言屏幕冻结、反复拖同一下都是正常的，唯有
        「当前值多轮没朝目标动」才是真卡住（方向错、步长恒不到一格、非法值回弹等）。
        """
        val = self._extract_progress_value(check)
        window = self._progress_values.setdefault(milestone.id, [])
        window.append(val)
        if len(window) > STUCK_VALUE_STALL_WINDOW:
            window.pop(0)
        if len(window) < STUCK_VALUE_STALL_WINDOW or not val:
            return None
        if len(set(window)) == 1:
            print(f"  [ValueStall] 连续 {STUCK_VALUE_STALL_WINDOW} 轮当前值停留「{val}」，未朝目标推进")
            # 触发后清空窗口：replan 会换新策略，让它重新攒满 STUCK_VALUE_STALL_WINDOW 轮再判，
            # 否则窗口仍满、下一轮值没立刻变就又立即 stall，新策略只有 1 轮机会、很快耗尽重试。
            window.clear()
            return _SingleCheckResult(
                status="stuck",
                reason=f"连续 {STUCK_VALUE_STALL_WINDOW} 轮当前值停留在「{val}」，调整未朝目标推进",
                stuck_reason="连续调值无进展：当前值多轮未变化",
                issues=["监控值多轮未朝目标推进（疑似方向错/步长不足/非法值回弹）"],
                summary=check.summary,
            )
        return None

    # ── Decompose & routing ───────────────────────────────────────────

    _MAX_DECOMPOSE_RETRIES = 2

    def _decompose(self, goal: str, observation: Observation) -> None:
        cfg = resolve_llm_config("supervisor.decompose")
        if not cfg.model:
            cfg = resolve_llm_config("supervisor")
        print(f"Supervisor: {cfg.provider} / {cfg.model}")
        llm = ChatOpenAI(model=cfg.model, api_key=cfg.api_key, base_url=cfg.base_url)

        issues: list[str] = []
        for attempt in range(self._MAX_DECOMPOSE_RETRIES + 1):
            self._do_decompose(llm, goal, observation, issues)
            issues = self._validate_decomposition(goal)
            if not issues:
                break
            if attempt < self._MAX_DECOMPOSE_RETRIES:
                print(f"  [Guard] 分解校验发现 {len(issues)} 项问题，重试 ({attempt+1}/{self._MAX_DECOMPOSE_RETRIES})...")
                for i in issues:
                    print(f"  [Guard]   {i}")

        self._patch_decomposition(llm, goal)

        if self._current_id not in self._milestones:
            self._current_id = self._next_milestone()

        print(f"任务分解为 {len(self._milestones)} 个子目标：")
        for mid in self._order:
            m = self._milestones[mid]
            deps = f" (依赖: {m.depends_on})" if m.depends_on else ""
            machine = "loop" if _is_loop(m) else "single"
            print(f"  [{m.id}][{machine}] {m.name}{deps}")
            print(f"       验收：{m.success_condition}")
            if m.scroll_stop_condition:
                print(f"       停止条件：{m.scroll_stop_condition}")

    def _do_decompose(
        self, llm: ChatOpenAI, goal: str, observation: Observation,
        feedback: list[str],
    ) -> None:
        msgs = self._msgs(DECOMPOSE_PROMPT, observation)
        user_parts: list[dict] = [{"type": "text", "text": f"用户任务：{goal}"}]
        if self._app_knowledge:
            user_parts.append({"type": "text", "text": f"\n## 应用导航知识\n{self._app_knowledge}"})
        if self._elements_knowledge:
            user_parts.append({"type": "text", "text": f"\n## 页面元素知识\n{self._elements_knowledge}"})
        if feedback:
            fb = "\n".join(f"  - {i}" for i in feedback)
            user_parts.append({"type": "text", "text": f"\n上一轮分解存在以下问题，请修正：\n{fb}"})
        msgs[1].content = user_parts + msgs[1].content
        resp = invoke_structured(llm, msgs, _DecomposeResponse)

        self._global_constraints = resp.global_constraints
        self.task_type = resp.task_type
        self._milestones = {m.id: m for m in resp.milestones}
        self._order = [m.id for m in resp.milestones]
        self._current_id = self._next_milestone()

    def _validate_decomposition(self, goal: str) -> list[str]:
        issues = []
        all_ids = set(self._milestones.keys())

        for m in self._milestones.values():
            for dep in m.depends_on:
                if dep not in all_ids:
                    issues.append(f"子目标「{m.name}」的 depends_on 包含不存在的 ID: {dep}")

        visited: set[str] = set()
        in_stack: set[str] = set()
        def _has_cycle(mid: str) -> bool:
            if mid in in_stack:
                return True
            if mid in visited:
                return False
            visited.add(mid)
            in_stack.add(mid)
            ms = self._milestones.get(mid)
            if ms:
                for dep in ms.depends_on:
                    if _has_cycle(dep):
                        return True
            in_stack.discard(mid)
            return False
        for mid in list(self._order):
            visited.clear()
            in_stack.clear()
            if _has_cycle(mid):
                issues.append(f"子目标之间存在循环依赖（从 {mid} 开始）")

        for m in self._milestones.values():
            if not m.success_condition.strip():
                issues.append(f"子目标「{m.name}」的验收条件为空")

        for m in self._milestones.values():
            if m.kind == "collection" and m.completion_strategy not in ("read_once", "scroll_until_boundary"):
                issues.append(f"子目标「{m.name}」kind=collection 但 completion_strategy={m.completion_strategy}，应为 read_once 或 scroll_until_boundary")

        for m in self._milestones.values():
            if m.completion_strategy == "scroll_until_boundary" and not m.scroll_stop_condition:
                issues.append(f"子目标「{m.name}」使用 scroll_until_boundary 但缺少 scroll_stop_condition")

        analysis_keywords = ("多少", "什么", "有没有", "查看", "看看", "统计", "查一下", "帮我找", "列出", "汇总", "比较")
        if self.task_type == "action" and any(kw in goal for kw in analysis_keywords):
            issues.append(f"task_type=action 但目标含查询关键词（{', '.join(kw for kw in analysis_keywords if kw in goal)}），应为 analysis")

        return issues

    def _patch_decomposition(self, llm: ChatOpenAI, goal: str) -> None:
        fixes = []

        verification_ids = [
            mid for mid, m in self._milestones.items()
            if m.kind == "verification"
        ]
        for vid in verification_ids:
            removed = self._milestones.pop(vid)
            self._order.remove(vid)
            for m in self._milestones.values():
                if vid in m.depends_on:
                    m.depends_on.remove(vid)
                    m.depends_on.extend(removed.depends_on)
            fixes.append(f"子目标「{removed.name}」（verification）已移除")

        all_ids = set(self._milestones.keys())
        for m in self._milestones.values():
            invalid = [d for d in m.depends_on if d not in all_ids]
            if invalid:
                m.depends_on = [d for d in m.depends_on if d in all_ids]
                fixes.append(f"子目标「{m.name}」移除无效依赖 {invalid}")

        visited: set[str] = set()
        in_stack: set[str] = set()
        def _has_cycle(mid: str) -> bool:
            if mid in in_stack:
                return True
            if mid in visited:
                return False
            visited.add(mid)
            in_stack.add(mid)
            ms = self._milestones.get(mid)
            if ms:
                for dep in ms.depends_on:
                    if _has_cycle(dep):
                        return True
            in_stack.discard(mid)
            return False
        for mid in self._order:
            visited.clear()
            in_stack.clear()
            if _has_cycle(mid):
                self._milestones[mid].depends_on = []
                fixes.append(f"清除子目标「{self._milestones[mid].name}」的依赖以打破循环")

        for m in self._milestones.values():
            if not m.success_condition.strip():
                m.success_condition = f"完成「{m.name}」"
                fixes.append(f"子目标「{m.name}」补全空的验收条件")

        for m in self._milestones.values():
            if m.kind == "collection" and m.completion_strategy not in ("read_once", "scroll_until_boundary"):
                m.completion_strategy = "scroll_until_boundary"
                fixes.append(f"子目标「{m.name}」策略修正为 scroll_until_boundary")

        scroll_milestones = [
            m for m in self._milestones.values()
            if m.completion_strategy == "scroll_until_boundary"
        ]
        for m in scroll_milestones:
            dep_context = ""
            if m.depends_on:
                dep_lines = []
                for dep_id in m.depends_on:
                    dep = self._milestones.get(dep_id)
                    if dep:
                        dep_lines.append(f"  - 前置子目标「{dep.name}」验收条件：{dep.success_condition}")
                if dep_lines:
                    dep_context = "\n".join(dep_lines)
            existing = f"\n当前停止条件：{m.scroll_stop_condition}" if m.scroll_stop_condition else "\n当前停止条件：（空）"
            patch = invoke_structured(
                llm,
                [
                    SystemMessage(content=STOP_CONDITION_PATCH_PROMPT),
                    HumanMessage(content=(
                        f"用户目标：{goal}\n"
                        f"子目标名称：{m.name}\n"
                        f"子目标描述：{m.description}\n"
                        f"本子目标验收条件：{m.success_condition}\n"
                        f"{dep_context}\n"
                        f"全局约束：{json.dumps(self._global_constraints, ensure_ascii=False)}"
                        f"{existing}"
                    )),
                ],
                _StopConditionPatch,
            )
            if patch.scroll_stop_condition != m.scroll_stop_condition:
                fixes.append(
                    f"子目标「{m.name}」停止条件修正：{m.scroll_stop_condition or '（空）'} → {patch.scroll_stop_condition}"
                )
                m.scroll_stop_condition = patch.scroll_stop_condition
                m.observable_boundary = patch.observable_boundary

        analysis_keywords = ("多少", "什么", "有没有", "查看", "看看", "统计", "查一下", "帮我找", "列出", "汇总", "比较")
        if self.task_type == "action" and any(kw in goal for kw in analysis_keywords):
            self.task_type = "analysis"
            fixes.append("task_type 从 action 修正为 analysis")

        if fixes:
            print(f"  [Guard] 补丁修复 {len(fixes)} 项：")
            for f in fixes:
                print(f"  [Guard]   {f}")

    def _next_milestone(self) -> Optional[str]:
        for mid in self._order:
            m = self._milestones[mid]
            if m.status != "pending":
                continue
            if all(self._milestones[dep].status == "done" for dep in m.depends_on):
                return mid
        return None

    def _llm(self) -> ChatOpenAI:
        return _make_llm()

    def _msgs(self, system_prompt: str, observation: Observation) -> list:
        return _build_msgs(system_prompt, observation.png_bytes)

    @staticmethod
    def _picker_drag_steps(plan: _PlanResult) -> Optional[int]:
        """从 planner 的结构化当前值/目标值算出 picker drag 要走的格数（绝对值）。

        仅在 drag_column 已填、且当前值与目标值都给出时生效。除返回格数外，还用这两个数
        直接定方向——同一列的数字比较永远有效，比从 instruction 文本正则抠数字更可靠（正则在
        指令只写目标值、如「直到显示21日」时会抠不到当前值而失效）。非 picker drag 或信息
        不全时返回 None，由下游回退到旧的文本解析路径。
        """
        if not plan.drag_column:
            return None
        cur, tgt = plan.drag_current_value, plan.drag_target_value
        if cur is None or tgt is None:
            return None
        if tgt != cur:
            plan.direction = "increase" if tgt > cur else "decrease"
        return abs(tgt - cur)

    @staticmethod
    def _fix_picker_direction(plan: _PlanResult) -> None:
        col = plan.drag_column or ""
        col_suffix = {"year": "年", "month": "月", "day": "日"}.get(col, "")
        if not col_suffix:
            return
        # 跨边界保护：拖某列时，若指令里**更高位的列**（拖 day→看 月/年，拖 month→看 年）
        # 出现两个不同的值，说明目标跨越了该列边界（如 3月31日→4月7日，月份 3≠4）。此时单看
        # 本列数字（31 vs 7）会得出相反方向，必须跳过翻转——正确做法是先对齐高位列（见 PLAN_PROMPT）。
        higher_suffixes = {"day": ["月", "年"], "month": ["年"], "year": []}.get(col, [])
        for hs in higher_suffixes:
            hvals = re.findall(rf"(\d+){hs}", plan.instruction)
            if len(hvals) >= 2 and len(set(hvals[:2])) > 1:
                print(f"  [Planner] direction fix 跳过：跨{hs}边界（{hvals[0]}{hs}→{hvals[1]}{hs}），本列数字比较无效")
                return
        nums = re.findall(rf"(\d+){col_suffix}", plan.instruction)
        if len(nums) < 2:
            return
        cur, tgt = int(nums[0]), int(nums[1])
        if tgt == cur:
            return
        correct = "increase" if tgt > cur else "decrease"
        if plan.direction != correct:
            print(f"  [Planner] direction fix: {plan.direction} → {correct} ({cur}→{tgt})")
            plan.direction = correct
            if correct == "increase":
                for old, new in [("向下拖动", "向上拖动"), ("下拉", "上拉"), ("往下", "往上")]:
                    plan.instruction = plan.instruction.replace(old, new)
            else:
                for old, new in [("向上拖动", "向下拖动"), ("上拉", "下拉"), ("往上", "往下")]:
                    plan.instruction = plan.instruction.replace(old, new)

    @staticmethod
    def _is_sequence(instruction: str) -> bool:
        text = instruction.strip()
        markers = ("操作序列", "步骤", "\n1.", "\n2.", "1.", "2.", "；2", ";2")
        return any(m in text for m in markers)

    def _is_repeated_instruction(
        self, instruction: str, milestone_id: str, history: list[PolicyTurn],
    ) -> bool:
        from difflib import SequenceMatcher
        _strip = lambda s: re.sub(r"[，。、；：""''《》\s（）\(\)]", "", s.strip())
        _scroll_words = ("滚动", "滑动", "拖动", "拖拽", "scroll", "drag")
        n_new = _strip(instruction)

        stuck_tried: set[str] = set()
        all_tried: list[str] = []
        for idx, t in enumerate(history):
            sv = t.supervisor
            if not sv or not sv.instruction:
                continue
            if sv.milestone_id == milestone_id:
                all_tried.append(sv.instruction)
            next_sv = history[idx + 1].supervisor if idx + 1 < len(history) else None
            if (
                next_sv
                and ("卡住" in (next_sv.summary or "") or "重试" in (next_sv.summary or ""))
            ):
                stuck_tried.add(sv.instruction)

        def _similar(new: str, old: str) -> bool:
            n_old = _strip(old)
            return bool(n_new and n_old) and SequenceMatcher(None, new, n_old).ratio() >= 0.6

        for old in stuck_tried:
            if _similar(n_new, old):
                return True

        if any(w in instruction for w in _scroll_words):
            return False
        similar_count = sum(1 for old in all_tried if _similar(n_new, old))
        return similar_count >= 2
