"""MilestoneSupervisorPolicy: two-machine milestone supervisor."""

import json
from typing import Literal, Optional

from langchain_openai import ChatOpenAI

from llm.structured import invoke_structured
from gui_agent.core.vision.frame_analysis import is_loading_frame
from gui_agent.core.schemas import Milestone, Observation, PolicyTurn, SupervisorStep
from gui_agent.core.self_learning.progressive import ProgressiveKnowledge, _norm as _norm_page

from .decomposition import MilestoneDecompositionMixin, _looks_like_analysis
from .helpers import _build_msgs, _format_history, _inject_knowledge, _make_llm, run_loop_check, run_planner
from .helpers import run_checker, run_selector, _default_milestone_prompts
from .runtime import (
    MAX_RETRIES,
    MAX_SCROLL_PER_MILESTONE,
    _Timer,
    _ctx,
    _default_read_instruction,
    _has_collected,
    _has_successful_scroll_for,
    _is_home_identity,
    _is_loop,
    _last_scroll_was_for,
    _type_only_search_filter_pending_submit,
)
from .schemas import (
    MilestonePrompts,
    _DecomposeResponse,
    _LoopFrameResult,
    _PlanResult,
    _ReplanResult,
    _SingleCheckResult,
    _StopConditionPatch,
)
from .stuck import (
    STUCK_REPEAT_WINDOW,
    STUCK_REPEAT_WORD_OVERLAP,
    STUCK_SCREEN_FROZEN,
    STUCK_SCREEN_SIMILARITY,
    STUCK_SCREEN_WINDOW,
    MilestoneStuckMixin,
)


# ── Main class ────────────────────────────────────────────────────────


class MilestoneSupervisorPolicy(MilestoneDecompositionMixin, MilestoneStuckMixin):
    """Two-machine milestone supervisor: single-step and loop run independently."""

    name = "milestone"

    def __init__(self, prompts: Optional[MilestonePrompts] = None) -> None:
        # Platform prompt set. Defaults to iphone's (lazy import) so every existing
        # no-arg constructor (iphone factory, evals, tests) is unchanged; browser can
        # inject its own once written — today it borrows iphone's.
        self._prompts = prompts or _default_milestone_prompts()
        self._global_constraints: list[str] = []
        self._milestones: dict[str, Milestone] = {}
        self._order: list[str] = []
        self._current_id: Optional[str] = None
        # One-shot: skip the next step()'s initial done-check and plan directly (set by reseed
        # for a freshly-entered navigation milestone — DAG _advance parity, see reseed).
        self._skip_initial_check: bool = False
        # Each entry pairs a frame with the action CENTER (normalized 0-1000 x/y, or None)
        # that produced it — the 局部 stuck tier inspects the region the agent touched.
        self._recent_screenshots: list[tuple[bytes, Optional[tuple[float, float]]]] = []
        self._scroll_counts: dict[str, int] = {}
        self.task_type: Literal["action", "analysis"] = "action"
        self._app_knowledge: Optional[str] = None
        self._check_knowledge: str = ""
        self._elements_knowledge: Optional[str] = None
        self._pk: Optional[ProgressiveKnowledge] = None  # progressive (skill-like) section loader
        self._app_name: str = ""
        self._last_url: Optional[str] = None  # 上一轮页面 URL(结构化跳页信号；浏览器才有)
        self._last_dom_state: Optional[str] = None  # 上一轮交互状态指纹(表单值+焦点；浏览器才有)
        self._dom_changed = False  # 本轮指纹是否变化 = 确定性进展信号(填表时像素/指令都相似)
        self._last_page_identity: dict[str, str] = {}
        self._last_check_summary: dict[str, str] = {}
        # 连续调值类的进展追踪：每 milestone 一个滑动窗口，存最近若干轮 checker 读到的「当前值」。
        self._progress_values: dict[str, list[str]] = {}
        self._last_check: Optional[_SingleCheckResult] = None
        self._milestone_done_checks: dict[str, "_SingleCheckResult"] = {}  # milestone_id → done check
        self._last_plan: Optional[_PlanResult] = None
        self._last_replan: Optional[_ReplanResult] = None
        self._last_sections_loaded: list[str] = []  # progressive section stems injected this turn (logged)
        # KnowledgeSelector cache: (milestone_id, normalized page_identity) → section stems.
        # Selection only changes when the page or the milestone changes, so within a key the
        # selector LLM is never re-invoked (empty selections are cached too).
        self._selector_cache: dict[tuple[str, str], list[str]] = {}
        self._goal: str = ""  # set by _decompose; selector prompt context
        self._timings: dict[str, float] = {}
        self._timings_order: list[str] = []
        self._token_usage: dict[str, dict[str, int]] = {}   # per-module {input, output}

    def set_app_knowledge(
        self,
        text: str,
        app_name: str = "",
        elements: str = "",
        sections: Optional[dict[str, str]] = None,
        check: str = "",
    ) -> None:
        self._app_knowledge = text
        self._elements_knowledge = elements or None
        # _check.md → Checker-only observable completion rules（动态验收知识）：描述该 app 界面
        # 的实际显示形态（列渲染短形式/成功提示样式/错误 toast 语义等）。静态 checker prompt 只留
        # 跨 app 通用原则；app 特定的验收事实从这里按 app 注入，避免静态规则膨胀和过拟合。
        self._check_knowledge = check
        # When per-section bodies exist, the planner loads them progressively: a dedicated
        # KnowledgeSelector micro-decision picks section ids per (milestone, page) — cached,
        # so it only fires on page/milestone changes. Falls back to the full `elements`
        # blob when absent.
        self._pk = ProgressiveKnowledge(sections) if sections else None
        self._selector_cache.clear()
        if app_name:
            self._app_name = app_name

    def runtime_state_snapshot(self) -> dict:
        """Return persisted runtime state without exposing supervisor internals."""
        return {
            "milestones": {
                mid: {
                    "status": m.status,
                    "retry_count": m.retry_count,
                }
                for mid, m in self._milestones.items()
            },
            "done_checks": {
                mid: check.model_dump(mode="json", exclude_none=True)
                for mid, check in self._milestone_done_checks.items()
            },
            "last_page_identity": dict(self._last_page_identity),
            "scroll_counts": dict(self._scroll_counts),
            "progress_values": {
                mid: list(values)
                for mid, values in self._progress_values.items()
            },
        }

    def step(self, observation: Observation, goal: str, history: list[PolicyTurn]) -> SupervisorStep:
        self._timings.clear()
        self._timings_order.clear()
        self._token_usage.clear()
        self._last_sections_loaded = []  # reset; _invoke_planner fills it when progressive knowledge is active
        # Report-only carry-overs: clear so a turn that DOESN'T run the planner/replan reports
        # null instead of the previous turn's stale plan (e.g. the terminal "done" turn). Both
        # are read solely by runner's _extract_plan/_extract_replan — no internal logic depends
        # on them persisting across turns (unlike _last_check, which is cross-turn memory).
        self._last_plan = None
        self._last_replan = None

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

    def reseed(
        self,
        milestone: Milestone,
        task_type: Literal["action", "analysis"] = "action",
        fresh_advance: bool = False,
    ) -> None:
        """单 milestone 模式（DSL orchestrator 用）：把 supervisor 重置成只驱动这一个 milestone，
        绕过内部 decompose（DSL program 已含全部 milestone，由解释器排序）。step() 把这个 milestone
        跑到 done 后，_current_id 自然走到 None → 下一次 step() 发 terminal/goal_completed，agent_loop
        据此向解释器要下一个并 reseed。

        与 DAG 模式的 `_advance` 对齐：跨 milestone **只清 `_recent_screenshots`**（stuck 检测器
        的帧历史，不该跨 milestone 串）；其余 per-milestone 态（page_identity / last_plan /
        last_replan / dom_state / done_checks / sections_loaded …）一律**保留**，与 `_advance`
        一致——否则编排器会比 DAG 更狠地清空，造成跨 milestone 上下文断裂。应用知识/全局约束本就
        保留（跨 milestone 复用）。task_type 控制读取门（读取类 milestone 传 'analysis' 才会真读）。"""
        self._milestones = {milestone.id: milestone}
        self._order = [milestone.id]
        self._current_id = milestone.id
        self.task_type = task_type  # 读取门：read milestone 须为 'analysis' 才读
        self._recent_screenshots.clear()  # 唯一要清的，和 _advance 一致
        # DAG `_advance` 的 nav 跳 check 镜像：fresh_advance=刚从上一个 milestone 推进过来（同帧），
        # 此时若新 milestone 是 navigation，它「in_progress by construction」，跳过首次验收直接规划
        # 第一步导航动作——省掉交接时第 2 次 checker。幂等（重点 nav 目标无害；残留 already-done 由
        # 下一轮正常 check 兜底）。action/filter/collection 一律保留 check：重跑 action 可能双执行
        # （re-send/re-submit），collection 需要 checker 的 read_instruction。一次性，step() 消费后清。
        self._skip_initial_check = fresh_advance and milestone.kind == "navigation"

    # ── Single-step machine ───────────────────────────────────────────

    def _run_single_turn(
        self,
        milestone: Milestone,
        observation: Observation,
        history: list[PolicyTurn],
    ) -> SupervisorStep:
        if is_loading_frame(observation):
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

        # Freshly entered navigation milestone (reseed fresh_advance, mirror DAG _advance):
        # skip the initial done-check and plan the first nav action directly — drops the 2nd
        # checker call on the milestone hand-off. One-shot.
        if self._skip_initial_check:
            self._skip_initial_check = False
            print("  [SkipCheck] 新进入导航子目标，跳过首次验收，直接规划")
            synthetic = _SingleCheckResult(
                status="in_progress",
                reason=f"刚进入子目标「{milestone.name}」，默认未完成",
                summary="",
            )
            self._last_check = synthetic
            self._last_page_identity[milestone.id] = ""
            return self._plan_single(milestone, synthetic, observation, history)

        if history and history[-1].action_decision:
            if history[-1].action_decision.action.action_type == "type":
                self._recent_screenshots.clear()

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

        if check.status == "done" and _type_only_search_filter_pending_submit(milestone, history):
            print("  [SubmitPending] 搜索/筛选只输入未提交，覆盖 done → in_progress")
            check = check.model_copy(update={
                "status": "in_progress",
                "reason": (
                    "最近一步只是输入搜索/筛选关键词，尚未看到 Search/Apply/Filter/Submit "
                    "或回车提交；当前列表/计数可能仍是提交前旧状态。"
                ),
                "summary": "搜索/筛选条件已填写，但还需要提交/应用。",
                "missing_evidence": ["需要点击 Search/Apply/Filter/Submit，或按回车提交搜索/筛选。"],
            })
            self._last_check = check

        current_page_id = check.page_identity or ""
        self._last_page_identity[milestone.id] = current_page_id

        # Programmatic page-change signal: the browser URL is ground truth, so a changed URL means
        # the previous action navigated — a definite EFFECT / page change. Use it to suppress the
        # pixel-based false positives below (false no_effect, false sim-stuck on a visually-similar
        # new page). None on iphone/android (url-less) → url_changed stays False, no effect there.
        cur_url = observation.url
        url_changed = bool(cur_url and self._last_url is not None and cur_url != self._last_url)
        if cur_url is not None:
            self._last_url = cur_url
        if url_changed:
            print(f"  [URLChanged] {cur_url} → 已跳页(确定性)，抑制 no_effect/sim_stuck 误判")

        # Second structural progress signal: the interactive-state fingerprint (form values +
        # focus). Filling a form field-by-field keeps pixels near-identical and produces
        # near-identical instructions ("在X输入框输入Y") — text similarity then misreads real
        # progress as a loop (20260612_103356: 8-step form fill escalated to stuck). A changed
        # fingerprint is ground truth that the last action DID something.
        cur_dom = observation.dom_state
        self._dom_changed = bool(cur_dom and self._last_dom_state is not None and cur_dom != self._last_dom_state)
        if cur_dom is not None:
            self._last_dom_state = cur_dom
        if self._dom_changed and not url_changed:
            print("  [DOMChanged] 交互状态指纹有变化(表单/焦点在推进)，抑制 stuck/重复误判")

        # VERIFY FIRST, before consuming the prior turn's off-target / no-effect signals.
        # An action that ACTUALLY satisfied the milestone advances even when the verifiers
        # misreported it — TargetVerify false off-target (tap hit the right tab but was read
        # as the wrong one) or settle false no-effect (the screen did change but under the
        # diff threshold). Only when NOT done do those signals route to replan; otherwise the
        # fast-paths (placed before the checker) would skip verification and replan a
        # milestone the action already completed. See logs/.../android/20260611_085000.
        if check.status == "done":
            final_read = None
            if milestone.kind in {"collection", "verification"} and self.task_type != "action":
                read_inst = check.read_instruction or _default_read_instruction(milestone)
                final_read = _ctx(milestone, read_inst)
            return self._advance(milestone, observation, history, final_read=final_read)

        # Off-target last action (post-action targeting verify said the tap missed) — and the
        # milestone is NOT done (checked above) → route straight into replan. Catches "screen
        # changed but to the wrong element" (e.g. 搜索框 tap hit 转账 tab), which SimStuck can't.
        last_tv = history[-1].target_verify if history else None
        if last_tv is not None and not last_tv.on_target:
            print(f"  [OffTarget] 上一步误中「{last_tv.actual_element}」，已先验收(未完成)→ replan")
            stuck = _SingleCheckResult(
                status="stuck",
                reason=f"上一步没有打开预期元素，当前停留在「{last_tv.actual_element}」相关状态",
                stuck_reason=f"上一步没有到达预期元素，当前显示「{last_tv.actual_element}」相关状态",
                summary="",
            )
            return self._handle_stuck(milestone, stuck, None, observation, history)

        # Ineffective last tap: target_verify said on_target (hit the intended element) yet
        # settle saw zero screen change → the tap did nothing (re-tapped an already-active tab
        # / inert element). Milestone not done (checked above) → replan now instead of waiting
        # ~3 frames for SimStuck. Complements off_target above (wrong-element vs
        # right-element-but-no-effect). Gated on tap so gestures (which legitimately may not
        # move much) never trigger it.
        last_turn = history[-1] if history else None
        if (
            last_turn is not None
            and getattr(last_turn, "no_effect", False)
            and not url_changed  # URL changed = the tap DID navigate → not a no-effect
            and not self._dom_changed  # interactive-state fingerprint moved = the tap DID something
            and last_turn.action_decision is not None
            and last_turn.action_decision.action.action_type in ("tap", "click")
            and (last_tv is None or last_tv.on_target)
        ):
            tapped = last_turn.action_decision.action.description or "目标元素"
            print(f"  [NoEffect] 上一步点击「{tapped}」落点正确但屏幕零变化，已先验收(未完成)→ replan")
            stuck = _SingleCheckResult(
                status="stuck",
                reason=f"点击「{tapped}」后页面没有出现验收条件所需的可见变化",
                stuck_reason=f"点击「{tapped}」后仍未看到目标状态，应尝试其他可见入口",
                summary="",
            )
            return self._handle_stuck(milestone, stuck, None, observation, history)

        prev_action = history[-1].action_decision.action if history and history[-1].action_decision else None

        # 连续调值类（picker/步进器收敛）：重复同一列滚动是正常的，但动作区连续不动不是正常进展。
        # 因此保留 SimStuck 的「全局+动作局部均无变化」判据；只有指令重复类 RepStuck 被抑制。
        # ValueStall 作为语义兜底：当画面在动但 checker 读到的当前值长期不变，也判停滞。
        if milestone.is_iterative:
            sim_stuck = None if url_changed else self._check_screen_similarity(observation, self._action_center(prev_action))
            if sim_stuck is not None:
                print(f"  [Stuck] {sim_stuck.status}: {sim_stuck.reason}")
                return self._handle_stuck(
                    milestone, sim_stuck, check.read_instruction, observation, history,
                    page_changed=False,
                    prev_page_id=prev_page_id, current_page_id=current_page_id,
                )
            self._last_check_summary[milestone.id] = check.summary
            stall = self._check_value_stall(milestone, check)
            if stall is not None:
                return self._handle_stuck(
                    milestone, stall, check.read_instruction, observation, history,
                    page_changed=False,
                    prev_page_id=prev_page_id, current_page_id=current_page_id,
                )
            return self._plan_single(milestone, check, observation, history)

        sim_stuck = None if (url_changed or self._dom_changed) else self._check_screen_similarity(observation, self._action_center(prev_action))
        self._last_check_summary[milestone.id] = check.summary

        rep_stuck = self._check_instruction_repetition(history, milestone.id) if not sim_stuck else None
        # Stepping a picker / value means repeating the SAME column scroll. The two-tier
        # _check_screen_similarity already returns stuck when the touched region is NOT moving,
        # so reaching here (sim_stuck is None) means the region IS changing — a repeated
        # value-adjust scroll is progress, not a loop. Don't let repetition flag it.
        if rep_stuck is not None and self._is_value_adjust(prev_action):
            print("  [RepStuck] 已抑制：调值类重复滚动属正常（动作区在变）")
            rep_stuck = None
        # Same exemption from ground truth: the interactive-state fingerprint changed since
        # last turn, so the "similar" instructions are advancing through a form, not looping.
        if rep_stuck is not None and self._dom_changed:
            print("  [RepStuck] 已抑制：交互状态指纹在变化（填表推进中）")
            rep_stuck = None
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
            if self._dom_changed:
                # Ground truth beats text similarity: the interactive-state fingerprint moved
                # since last turn, so the prior "similar" instruction WORKED (form filling
                # produces legitimately alike instructions). Let the plan through unchanged.
                print("  [Planner] 指令相似但交互状态在推进（DOM 有变化），放行")
            else:
                print("  [Planner] 指令重复已失败操作，重试...")
                with _Timer(self._timings, self._timings_order, "planner", self._token_usage):
                    plan = self._invoke_planner(
                        milestone, check, observation, history,
                        extra=(
                            "你刚才的指令与之前未达成验收条件的操作相同。"
                            "请仔细查看截图，找一个不同的 UI 元素或操作路径。"
                        ),
                    )
                if self._is_repeated_instruction(plan.instruction, milestone.id, history):
                    print("  [Planner] 重试仍重复，升级为 stuck 处理")
                    stuck_check = _SingleCheckResult(
                        status="stuck",
                        reason="连续给出相似操作，当前页面仍未满足验收条件",
                        stuck_reason="连续给出相似指令但目标未达成，需要改用当前截图中的其他可见入口或操作顺序",
                        issues=["连续操作策略过于相似"],
                        summary=check.summary,
                    )
                    return self._handle_stuck(milestone, stuck_check, check.read_instruction, observation, history)
        # 结构化 drag 距离：当 planner 给出本列的当前值/目标值时，按差几格算出 drag_steps，
        # 让 action policy 据此放大拖动幅度（差 20 格用 large 粗调、接近后自动收回 small 精调）。
        # 这条结构化通路绕开「从 instruction 文本正则抠数字」的脆弱性——指令只写了目标值
        # （如「直到显示21日」缺当前值）时，正则抠不到距离会退化成一格一格挪、跑满轮数也到不了。
        drag_steps = self._picker_drag_steps(plan)
        drag_column = getattr(plan, "drag_column", None)
        if drag_steps == 0 and drag_column:
            print("  [Planner] picker 已到目标值但仍要求滚动，重试...")
            with _Timer(self._timings, self._timings_order, "planner", self._token_usage):
                plan = self._invoke_planner(
                    milestone,
                    check,
                    observation,
                    history,
                    extra=(
                        "你刚才要求滚动 picker，但结构化字段显示该列当前值已经等于目标值（steps=0）。"
                        "禁止继续滚动/拖动这个 picker 列，也不要为了“确认精确”微调。"
                        "请改为当前子目标剩余的一个动作：如果需要提交结果，就点击保存/确认/应用/完成/下一步等按钮；"
                        "如果还有其他未到位的 picker 列，只能操作那一列；如果验收已经满足则停止。"
                    ),
                )
            drag_steps = self._picker_drag_steps(plan)
            drag_column = getattr(plan, "drag_column", None)
            if drag_steps == 0 and drag_column:
                print("  [Planner] 重试仍为零步 picker，清空 picker hints 避免执行层强制滚动")
                plan.direction = None
                plan.drag_column = None
                plan.drag_current_value = None
                plan.drag_target_value = None
                drag_steps = None
        print(f"  [Planner] {plan.instruction}")
        drag_column = getattr(plan, "drag_column", None)
        if drag_steps is not None and drag_column:
            print(f"  [Planner] hints: direction={plan.direction} column={drag_column} steps={drag_steps}")
        elif plan.direction or drag_column:
            print(f"  [Planner] hints: direction={plan.direction} column={drag_column}")
        if plan.direction in ("increase", "decrease") and drag_column:
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
            drag_column=getattr(plan, "drag_column", None),
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
        # Persist this milestone's DONE verdict before _last_check is overwritten by the next
        # milestone's check (the report's 验收 panel renders it via context.milestones[id].
        # done_check). Must be here, not only in the nav-skip branch below: the terminal path
        # (orchestrator single-milestone completion → _next_milestone() is None) and the
        # non-nav next path both reach the bottom without otherwise saving it, which left the
        # acceptance panel empty for those milestones.
        if self._last_check is not None:
            self._milestone_done_checks[milestone.id] = self._last_check
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
            # (done check for the completed milestone already saved at the top of _advance)
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
                        if not (
                            c.startswith("指令「")
                            and ("禁止重复此指令" in c or "未达成目标" in c)
                        )
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
        # the next Replan prompt) avoids immediately repeating the same unproductive path.
        if replan.diagnosis:
            path_constraint = f"⚠️ 之前未达成目标的路径：{replan.diagnosis}。除非当前截图出现新的明确证据，否则不要重复。"
            if path_constraint not in self._global_constraints:
                self._global_constraints.append(path_constraint)

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
            summary=f"子目标「{milestone.name}」尚未达成，第 {milestone.retry_count} 次调整策略。{replan.diagnosis}",
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
        if "planner 陷入重复" in reason or "连续给出相似指令" in reason:
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
        constraint = f"指令「{instruction}」未达成目标：{reason}。优先尝试当前截图中不同的可见入口。"
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
            prompts=self._prompts,
            check_knowledge=self._check_knowledge,
        )

    def _loop_check(
        self,
        milestone: Milestone,
        observation: Observation,
        history: list[PolicyTurn],
    ) -> _LoopFrameResult:
        return run_loop_check(
            milestone, observation, history, constraints=self._global_constraints,
            prompts=self._prompts,
        )

    def _select_sections(self, milestone: Milestone, check: _SingleCheckResult) -> list[str]:
        """Resolve which knowledge sections to inject this turn, via the KnowledgeSelector.

        Cache key = (milestone id, normalized page_identity): selection only changes when
        the page or the milestone changes, so most turns reuse the cached stems and cost
        nothing. Empty selections are cached too (a page with no relevant sections should
        not retry every turn). On selector failure nothing is cached — the turn falls back
        to the zero-cost page_identity fuzzy match and the next turn retries the LLM."""
        if self._pk is None:
            return []
        page_id = check.page_identity or ""
        key = (milestone.id, _norm_page(page_id))
        if key in self._selector_cache:
            return self._selector_cache[key]
        try:
            with _Timer(self._timings, self._timings_order, "selector", self._token_usage):
                sel = run_selector(
                    self._goal, milestone, page_id,
                    self._pk.selector_manifest(),
                    prompts=self._prompts,
                )
            stems = self._pk.by_ids(sel.section_ids)
            if stems or sel.section_ids:
                names = "、".join(stems) if stems else "（ID 未命中）"
                print(f"  [Selector] {names}" + (f" — {sel.reason}" if sel.reason else ""))
            self._selector_cache[key] = stems
            return stems
        except Exception as exc:  # noqa: BLE001 — selector must never block the planner
            print(f"  [Selector] 调用失败，回退 page_identity 模糊匹配：{exc}")
            return self._pk.pick([], page_id)

    def _elements_for(self, milestone: Milestone, check: _SingleCheckResult) -> Optional[str]:
        """Element knowledge for instruction generation: the per-section bodies the
        KnowledgeSelector picked for this (milestone, page) — cached — when section knowledge
        exists, else the full _elements.md blob as fallback. Shared by planner and replanner so
        both inject the same focused slice instead of the whole 600+-line elements blob."""
        if self._pk:
            self._last_sections_loaded = self._select_sections(milestone, check)
            return self._pk.bodies(self._last_sections_loaded)
        return self._elements_knowledge

    def _invoke_planner(
        self,
        milestone: Milestone,
        check: _SingleCheckResult,
        observation: Observation,
        history: list[PolicyTurn],
        extra: str = "",
    ) -> _PlanResult:
        elements = self._elements_for(milestone, check)
        return run_planner(
            milestone, check, observation, history,
            constraints=self._global_constraints,
            extra=extra,
            app_knowledge=self._app_knowledge,
            elements_knowledge=elements,
            prompts=self._prompts,
        )

    def _invoke_loop_scroll(
        self,
        milestone: Milestone,
        frame: _LoopFrameResult,
        observation: Observation,
    ) -> _PlanResult:
        prompt = self._prompts.loop_scroll.format(
            milestone_name=milestone.name,
            milestone_desc=milestone.description,
            constraints=json.dumps(self._global_constraints, ensure_ascii=False),
            frame_summary=frame.summary,
        )
        plan_schema = self._prompts.plan_result_schema or _PlanResult
        return invoke_structured(self._llm(), self._msgs(prompt, observation), plan_schema)

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
        prompt = self._prompts.replan.format(
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
        _inject_knowledge(msgs, self._app_knowledge, self._elements_for(milestone, check))
        result = invoke_structured(self._llm(), msgs, _ReplanResult)
        if self._is_sequence(result.instruction):
            print("  [Replan] 多步序列，重试...")
            result = self._invoke_replanner(
                milestone, check, observation, history,
                extra="你刚才输出了多个步骤，请只返回一个原子操作。",
            )
        return result

    def _llm(self) -> ChatOpenAI:
        return _make_llm()

    def _msgs(self, system_prompt: str, observation: Observation) -> list:
        return _build_msgs(system_prompt, observation.png_bytes, image_resize=self._prompts.image_resize)
