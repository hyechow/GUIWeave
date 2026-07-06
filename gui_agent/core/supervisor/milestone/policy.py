"""MilestoneSupervisorPolicy: two-machine milestone supervisor."""

import re
from typing import Literal, Optional

from langchain_openai import ChatOpenAI

from gui_agent.context.runtime import (
    completed_milestones_block,
    constraints_block,
    extra_instruction_block,
    form_controls_block,
    history_block,
    knowledge_block,
    loop_frame_summary_block,
    milestone_block,
    replan_state_block,
    tried_instructions_block,
)
from llm.structured import invoke_structured
from gui_agent.core.vision.frame_analysis import is_loading_frame
from gui_agent.core.schemas import Milestone, Observation, PolicyTurn, SupervisorStep
from gui_agent.core.self_learning.progressive import ProgressiveKnowledge, _norm as _norm_page

from .decomposition import MilestoneDecompositionMixin, _looks_like_analysis
from .helpers import assemble_messages, _make_llm, run_loop_check, run_planner
from .helpers import run_checker, run_selector, _default_milestone_prompts, is_dispatch_gate_sc
from .helpers import (
    checkbox_toggle_satisfies_target,
    filter_chips_clean,
    filter_state_satisfies_target,
    native_select_satisfies_target,
)
from .runtime import (
    EARLY_FEASIBILITY_AT,
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
from gui_agent.core.run.progress_monitor import ProgressMonitor, action_signature, canonical_url
from .schemas import (
    MilestonePrompts,
    _DecomposeResponse,
    _LoopFrameResult,
    _PlanResult,
    _ReplanResult,
    _SingleCheckResult,
    _StopConditionPatch,
)
from .stuck import MilestoneStuckMixin


# ── Main class ────────────────────────────────────────────────────────


# Substrings (post-normalization) that mark "page not identified". The KnowledgeSelector keys
# on page_identity, so an empty pick under an unidentified page must NOT be cached (it would
# turn knowledge OFF for the rest of the milestone exactly when the key is weakest). Matched as
# SUBSTRINGS, not exact: the checker writes free-form text ("无法识别当前页面", "未知页面(用户中心?)",
# "unknown page" → "unknownpage"), so exact-set membership misses every real-world variant.
_UNKNOWN_PAGE_MARKERS = ("未知", "未识别", "无法识别", "不确定", "unknown", "unidentified")
_TARGET_IDENTITY_MARKER = "必须对应子目标指定对象"
_TARGET_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{1,}")
_URL_TEXT_RE = re.compile(r"https?://[^\s「」'\"<>]+")
_RESOURCE_ID_MARKERS = (
    "id",
    "entity_id",
    "product_id",
    "item_id",
    "order_id",
    "customer_id",
    "review_id",
)
_PLAN_INPUT_LIKE_RE = re.compile(
    r"(?:"
    r"输入|填入|填写|录入|键入|重输|重新输入|覆盖输入|"
    r"清空.*(?:输入|填入|填写|录入)|删除.*(?:输入|填入|填写|录入)|"
    r"(?:type|fill|enter\s+(?:text|value)|replace\s+(?:text|value)?|set\s+.*(?:field|input|value))"
    r")",
    re.IGNORECASE,
)
_PLAN_QUOTED_VALUE_RE = re.compile(r"[「『“\"'`]([^」』”\"'`]{1,120})[」』”\"'`]")
_PLAN_UNQUOTED_INPUT_VALUE_RE = re.compile(
    r"(?:输入|填入|填写|录入|键入|重输|重新输入|覆盖输入)\s*([^，。；;]+)$",
    re.IGNORECASE,
)


def _plan_is_input_like(instruction: str) -> bool:
    """Whether the next planner instruction is another concrete text/value entry."""
    return bool(_PLAN_INPUT_LIKE_RE.search(instruction or ""))


def _norm_plan_input_value(value: str) -> str:
    text = (value or "").lower()
    text = re.sub(r"[\s，。、；;：:！!？?（）()「」『』\[\]【】\"'`’‘“”]+", "", text)
    return text[:80]


def _plan_input_value(instruction: str) -> str:
    text = instruction or ""
    quoted = [m.group(1).strip() for m in _PLAN_QUOTED_VALUE_RE.finditer(text) if m.group(1).strip()]
    if quoted:
        return _norm_plan_input_value(quoted[-1])
    match = _PLAN_UNQUOTED_INPUT_VALUE_RE.search(text)
    if match:
        return _norm_plan_input_value(match.group(1).strip())
    return ""


def _type_repeat_matches_current_plan(signature: str, instruction: str) -> bool:
    """True only when the repeated type signature is the value this plan is about to type again."""
    repeated_value = (signature or "").rsplit("|", 1)[-1]
    planned_value = _plan_input_value(instruction)
    return not planned_value or planned_value == repeated_value


def _page_known(page_identity: str) -> bool:
    n = _norm_page(page_identity)
    return bool(n) and not any(marker in n for marker in _UNKNOWN_PAGE_MARKERS)


def _target_identity_hint(milestone: Milestone, observation: Observation) -> str:
    """Expose exact machine-route identity only for runtime-targeted milestones.

    Foreach/detail workflows often target an object whose stable id is present in the
    browser route but not visible in the viewport. The generic checker should be able
    to use that route identity when the milestone itself already contains an explicit
    target-identity gate; do not inject arbitrary URLs for ordinary page checks.
    """
    if _TARGET_IDENTITY_MARKER not in (milestone.success_condition or ""):
        return ""
    url = str(getattr(observation, "url", "") or "")
    if not url:
        return ""
    text = f"{milestone.name}\n{milestone.success_condition}"
    tokens: list[str] = []
    seen: set[str] = set()
    for token in _TARGET_TOKEN_RE.findall(text):
        if not any(ch.isdigit() for ch in token):
            continue
        lowered = token.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        tokens.append(token)
    matches = [
        token for token in tokens[:8]
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(token)}(?![A-Za-z0-9])", url)
    ]
    if not matches:
        return ""
    return (
        "系统机器状态补充：当前浏览器 URL/路由精确包含当前子目标的目标标识"
        f"「{'、'.join(matches[:3])}」。判断是否打开了指定对象详情/结果页时，"
        "这个路由标识就是对象身份的机器可观测证据；若当前页已经是详情/结果页且验收字段可见，"
        "不得因为字段值（如名称、昵称、评分、状态）与预期筛选结果不同而否定对象身份。"
        "字段取值仍按当前页面可见内容和结构化读取判定，后续筛选/汇总步骤负责决定这些字段值是否符合最终任务条件。"
    )


def _resource_identity_from_url(url: str | None) -> str:
    """Return a generic row/entity identity from detail-style URLs.

    This intentionally avoids app-specific words. It recognizes common resource-id routes
    such as ``.../edit/id/1843`` and falls back to the last digit-bearing path token only on
    detail-like routes. List/filter/paging URLs should not become row scopes.
    """
    path = canonical_url(url)
    if not path:
        return ""
    parts = [part for part in path.split("/") if part]
    if not parts:
        return ""
    lower = [part.lower() for part in parts]
    for marker in _RESOURCE_ID_MARKERS:
        for i, part in enumerate(lower[:-1]):
            if part == marker and parts[i + 1]:
                prefix = "/".join(parts[: i + 2])
                return prefix.lower()
    detail_like = any(part in {"edit", "view", "detail", "details", "show"} for part in lower)
    if detail_like:
        for i in range(len(parts) - 1, -1, -1):
            token = parts[i]
            if any(ch.isdigit() for ch in token):
                return "/".join(parts[: i + 1]).lower()
    return ""


def _resource_identity_from_text(text: str) -> str:
    """Extract a row/entity identity from rendered milestone text when it embeds a URL."""
    for match in _URL_TEXT_RE.finditer(text or ""):
        ident = _resource_identity_from_url(match.group(0))
        if ident:
            return ident
    return ""


def _turn_execution_scope(turn: PolicyTurn) -> str:
    sv = getattr(turn, "supervisor", None)
    return str(getattr(sv, "execution_scope", "") or "") if sv else ""


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
        self._scroll_counts: dict[str, int] = {}
        self.task_type: Literal["action", "analysis"] = "action"
        self._app_knowledge: Optional[str] = None
        self._check_knowledge: str = ""
        self._elements_knowledge: Optional[str] = None
        self._pk: Optional[ProgressiveKnowledge] = None  # progressive (skill-like) section loader
        self._app_name: str = ""
        # url/dom-delta effect signals now live on the ProgressMonitor (observe_effect).
        self._last_page_identity: dict[str, str] = {}
        self._last_check_summary: dict[str, str] = {}
        # 连续调值进展窗口、url/dom 效果信号、帧/指令/值停滞探测都在 ProgressMonitor 上。
        # Action-Loop Guard: run-scoped (state, action) memory keyed on canonical URL — catches task-level
        # loops the frame guards miss. NOT reset per milestone (a loop can span milestone boundaries).
        self._monitor = ProgressMonitor()
        self._early_feasibility_probed: set[str] = set()  # milestone ids given an early Feasibility probe
        self._last_check: Optional[_SingleCheckResult] = None
        self._collection_progress: str = ""
        self._collection_done: bool = False
        self._milestone_done_checks: dict[str, "_SingleCheckResult"] = {}  # milestone_id → done check
        self._last_plan: Optional[_PlanResult] = None
        self._last_replan: Optional[_ReplanResult] = None
        self._last_sections_loaded: list[str] = []  # progressive section stems injected this turn (logged)
        self._context_reports: list[dict] = []       # context/selector decisions for this turn
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
                for mid, values in self._monitor._progress_values.items()
            },
        }

    def _execution_scope_for(self, milestone: Milestone, observation: Observation) -> str:
        """Bucket runtime memory by row/entity when visible, else by milestone.

        Foreach/detail workflows often reuse the same action template on several rows. The
        current detail URL is the most reliable row identity; rendered milestone text is a
        fallback for direct navigation steps before the browser has moved.
        """
        identity = _resource_identity_from_url(getattr(observation, "url", None))
        if not identity:
            identity = _resource_identity_from_text(
                f"{milestone.name}\n{milestone.description}\n{milestone.success_condition}"
            )
        if identity:
            return f"row:{identity}"
        return f"milestone:{milestone.id}"

    def _history_for_scope(
        self,
        history: list[PolicyTurn],
        milestone: Milestone,
        observation: Observation,
    ) -> list[PolicyTurn]:
        scope = self._execution_scope_for(milestone, observation)
        if any(_turn_execution_scope(t) for t in history):
            return [t for t in history if _turn_execution_scope(t) == scope]
        # Legacy tests / pre-scope contexts: preserve the old milestone-local behavior.
        return [
            t for t in history
            if getattr(getattr(t, "supervisor", None), "milestone_id", None) == milestone.id
        ]

    def _history_for_current_milestone(
        self,
        history: list[PolicyTurn],
        milestone: Milestone,
        observation: Observation,
    ) -> list[PolicyTurn]:
        scoped = self._history_for_scope(history, milestone, observation)
        return [
            t for t in scoped
            if getattr(getattr(t, "supervisor", None), "milestone_id", None) == milestone.id
        ]

    def note_executed_action(
        self,
        *,
        index: int,
        observation: Observation,
        supervisor_step: SupervisorStep,
        action_decision,
        executed: bool,
    ) -> None:
        """Record the concrete DOM-backed action after executor snapping.

        Planner instructions are only intent text. On browser pages the executor can attach a
        snapped DOM target to the action, so the repeat key should be the concrete action
        signature plus the current DOM/form fingerprint. This keeps repeated instructions for
        different rows/pages from looking identical while still surfacing true same-state loops.
        """
        if not executed or action_decision is None:
            return
        if getattr(supervisor_step, "completion_strategy", None) in {
            "repeat_until_satisfied",
            "scroll_until_boundary",
            "react_until_collected",
        }:
            return
        action = getattr(action_decision, "action", None)
        if action is None:
            return
        state = canonical_url(getattr(observation, "url", None))
        dom_state = getattr(observation, "dom_state", None) or ""
        snap = getattr(action, "snap", None)
        if not state or (not dom_state and not snap):
            return
        decision = action_signature(action)
        scope = getattr(supervisor_step, "execution_scope", "") or ""
        hit = self._monitor.repeated(state, decision, dom_state, scope)
        self._monitor.note(index, state, decision, dom_state, scope)
        if hit is not None:
            print(f"  [LoopGuard] 同 DOM 状态重复执行了 T{hit.index} 的同一动作签名 → 记录为打转事实")
            con = (
                f"⚠️ 当前执行上下文下已经执行过同一具体动作（{decision}）且目标未达成。"
                "必须换一个当前页面可见的新入口或新操作，禁止重复同一 DOM 目标。"
            )
            if con not in self._global_constraints:
                self._global_constraints.append(con)

    def step(self, observation: Observation, goal: str, history: list[PolicyTurn]) -> SupervisorStep:
        self._timings.clear()
        self._timings_order.clear()
        self._token_usage.clear()
        self._last_sections_loaded = []  # reset; _invoke_planner fills it when progressive knowledge is active
        self._context_reports = []
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
        result_ms = self._milestones.get(result.milestone_id or "", milestone)
        result.execution_scope = self._execution_scope_for(result_ms, observation)

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
        self._monitor.clear_screenshots()  # 唯一要清的，和 _advance 一致
        # DAG `_advance` 的 nav 跳 check 镜像：fresh_advance=刚从上一个 milestone 推进过来（同帧），
        # 此时若新 milestone 是 navigation，它「in_progress by construction」，跳过首次验收直接规划
        # 第一步导航动作——省掉交接时第 2 次 checker。幂等（重点 nav 目标无害；残留 already-done 由
        # 下一轮正常 check 兜底）。action/filter/collection 一律保留 check：重跑 action 可能双执行
        # （re-send/re-submit），collection 需要 checker 的 read_instruction。一次性，step() 消费后清。
        # precondition（入口状态归一化门，如 loop/function 体的「确保在列表页」）例外：它本就可能
        # 第一帧即满足，必须让 checker 先判（满足→done、不动作、不发 stop；未满足→in_progress→planner
        # 才规划返回动作），否则 SkipCheck 会把分支判断泄漏给 planner（live 185 的 stop / 错域 selector）。
        self._skip_initial_check = (
            fresh_advance and milestone.kind == "navigation" and not milestone.precondition
        )

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

        execution_scope = self._execution_scope_for(milestone, observation)
        scoped_history = self._history_for_scope(history, milestone, observation)
        milestone_history = self._history_for_current_milestone(history, milestone, observation)

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
            return self._plan_single(milestone, synthetic, observation, scoped_history)

        if milestone_history and milestone_history[-1].action_decision:
            if milestone_history[-1].action_decision.action.action_type == "type":
                self._monitor.clear_screenshots()

        prev_page_id = self._last_page_identity.get(milestone.id, "")

        # Observe state-change signals first — these are deterministic ground truth.
        # url_changed / dom_changed feed both the dispatch gate check below and the
        # stuck/no_effect suppression further down.
        self._monitor.observe_effect(observation.url, observation.dom_state)
        if self._monitor.url_changed:
            print(f"  [URLChanged] {observation.url} → 已跳页(确定性)，抑制 no_effect/sim_stuck 误判")
        if self._monitor.dom_changed and not self._monitor.url_changed:
            print("  [DOMChanged] 交互状态指纹有变化(表单/焦点在推进)，抑制 stuck/重复误判")

        # Dispatch gate: success_condition asks only "did the action produce any UI response?"
        # That is a deterministic question — url_changed is conclusive (page navigation is
        # unambiguous). dom_changed alone is NOT used: it fires on any form fill / datepicker
        # set / focus change, which would short-circuit before the submit action runs.
        if is_dispatch_gate_sc(milestone.success_condition) and self._monitor.url_changed:
            check = _SingleCheckResult(
                status="done",
                reason="动作已发出且界面响应已确认（URL/DOM 状态变化，确定性信号）",
                summary="dispatch gate 满足",
            )
            print("  [DispatchGate] 确定性响应信号 → done（跳过 LLM 验收）")
            self._last_check = check
            return self._advance(milestone, observation, history)

        # Filter "action-applied" gate — generalizes the dispatch gate to filter milestones.
        # A `filter` milestone's job is to APPLY a filter; the grid's Active-filters chips
        # (Observation.applied_filters) report authoritatively whether the intended filter is in
        # effect, independent of which rows/columns are rendered. So "动作是否生效" is decided by
        # the control's own state, NOT by re-reading row content — the checker once conflated a
        # display column (Magento Salable Quantity) with the filtered Quantity and rejected a
        # correctly-applied `Quantity: 3 - 3` into a clear→reset loop (run 20260629_173028).
        # `filter_chips_clean` keeps the gate from masking an unrelated residual the milestone
        # still owes a clear for (task 186 class). Skips the LLM checker like the dispatch gate.
        applied_filters = getattr(observation, "applied_filters", None)
        if (
            milestone.kind == "filter"
            and filter_state_satisfies_target(applied_filters, milestone)
            and filter_chips_clean(applied_filters, milestone)
        ):
            check = _SingleCheckResult(
                status="done",
                reason="目标筛选已生效（Active filters chip 命中，确定性状态信号）；行内容由筛选编码，无需逐行复核",
                summary="filter applied gate 满足",
            )
            print(f"  [FilterGate] 目标筛选已生效 {applied_filters} → done（跳过 LLM 验收）")
            self._last_check = check
            return self._advance(milestone, observation, history)

        # Native-select value gate — same signal protocol as the filter gate, on obs.dom. A native
        # <select>'s selection is DOM-authoritative (form_controls.selected_text); the vision checker
        # provably loops "still-open list box = not selected" despite the DOM even with the labeled
        # arbitration protocol (WebArena 702 Customer Groups). When a select-focused milestone's
        # target value is already selected, that is deterministic ground truth.
        if native_select_satisfies_target(getattr(observation, "form_controls", None), milestone):
            check = _SingleCheckResult(
                status="done",
                reason="目标 native select 的已选值已满足（obs.dom selected_text 权威，确定性信号）；列表框常开不代表未选中",
                summary="native select value gate 满足",
            )
            print("  [NativeSelectGate] DOM selected_text 满足目标 → done（跳过 LLM 视觉误判）")
            self._last_check = check
            return self._advance(milestone, observation, history)

        # Checkbox/switch value gate — panel overlays can hide the downstream visual result
        # (e.g. a grid header) while the AX/DOM control state is already checked. Re-clicking an
        # already-checked toggle can revert it, so treat a matched ON toggle as deterministic done
        # for checkbox-focused enable/show/select milestones.
        if checkbox_toggle_satisfies_target(
            getattr(observation, "form_controls", None),
            getattr(observation, "semantic_tree", None),
            milestone,
        ):
            check = _SingleCheckResult(
                status="done",
                reason="目标 checkbox/switch 已处于选中状态（obs.dom/AX checked 状态权威，确定性信号）；浮层遮挡不应触发重复点击",
                summary="checkbox toggle gate 满足",
            )
            print("  [CheckboxGate] DOM/AX checked 状态满足目标 → done（跳过 LLM 视觉误判）")
            self._last_check = check
            return self._advance(milestone, observation, history)

        with _Timer(self._timings, self._timings_order, "checker", self._token_usage):
            check = self._single_check(
                milestone,
                observation,
                scoped_history,
                execution_scope=execution_scope,
                effect_history=milestone_history,
            )
        self._last_check = check
        print(f"  [SingleCheck] {check.status}: {check.reason}")

        if check.loading:
            print("  [Loading] 检测到加载状态，等待下一帧...")
            return SupervisorStep(
                should_act=False, instruction=None, stop=False,
                goal_completed=False, is_loading=True, summary="页面加载中，等待...",
                **_ctx(milestone, None),
            )

        if check.status == "done" and _type_only_search_filter_pending_submit(milestone, milestone_history):
            print("  [SubmitPending] 搜索/筛选只输入未提交，覆盖 done → in_progress")
            check = check.model_copy(update={
                "status": "in_progress",
                "reason": (
                    "最近一步只是输入搜索/筛选关键词，尚未看到提交/应用筛选/确认搜索"
                    "或回车提交；当前列表/计数可能仍是提交前旧状态。"
                ),
                "summary": "搜索/筛选条件已填写，但还需要提交/应用。",
                "missing_evidence": ["需要点击页面上的提交/搜索/应用筛选控件，或按回车提交搜索/筛选。"],
            })
            self._last_check = check

        current_page_id = check.page_identity or ""
        self._last_page_identity[milestone.id] = current_page_id

        if milestone.completion_strategy == "react_until_collected":
            if self._collection_done:
                final_read = None
                if self.task_type != "action":
                    read_inst = check.read_instruction or _default_read_instruction(milestone)
                    final_read = _ctx(milestone, read_inst)
                return self._advance(milestone, observation, history, final_read=final_read)
            if check.status in {"done", "stuck"}:
                print(f"  [Collect] controller 未完成，覆盖 checker {check.status} → in_progress")
                check = check.model_copy(update={"status": "in_progress"})

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

        # The checker itself judged NO PROGRESS (status=stuck) from the 任务进展轨迹 (PROGRESS half of
        # the checker): the agent is looping / dead-ending, not advancing. Route to the stuck path —
        # replan + the early Feasibility probe — the same as the deterministic detectors.
        if check.status == "stuck":
            print(f"  [Checker] 判定无进展(stuck)：{check.stuck_reason or check.reason}")
            return self._handle_stuck(milestone, check, check.read_instruction, observation, scoped_history)

        if milestone.completion_strategy == "react_until_collected":
            return self._plan_single(milestone, check, observation, scoped_history)

        # Off-target last action (post-action targeting verify said the tap missed) — and the
        # milestone is NOT done (checked above) → route straight into replan. Catches "screen
        # changed but to the wrong element" (e.g. 搜索框 tap hit 转账 tab), which SimStuck can't.
        last_tv = milestone_history[-1].target_verify if milestone_history else None
        if last_tv is not None and not last_tv.on_target:
            print(f"  [OffTarget] 上一步误中「{last_tv.actual_element}」，已先验收(未完成)→ replan")
            stuck = _SingleCheckResult(
                status="stuck",
                reason=f"上一步没有打开预期元素，当前停留在「{last_tv.actual_element}」相关状态",
                stuck_reason=f"上一步没有到达预期元素，当前显示「{last_tv.actual_element}」相关状态",
                summary="",
            )
            return self._handle_stuck(milestone, stuck, None, observation, scoped_history)

        # Ineffective last tap: target_verify said on_target (hit the intended element) yet
        # settle saw zero screen change → the tap did nothing (re-tapped an already-active tab
        # / inert element). Milestone not done (checked above) → replan now instead of waiting
        # ~3 frames for SimStuck. Complements off_target above (wrong-element vs
        # right-element-but-no-effect). Gated on tap so gestures (which legitimately may not
        # move much) never trigger it.
        last_turn = milestone_history[-1] if milestone_history else None
        if (
            last_turn is not None
            and getattr(last_turn, "no_effect", False)
            and not self._monitor.url_changed  # URL changed = the tap DID navigate → not a no-effect
            and not self._monitor.dom_changed  # interactive-state fingerprint moved = the tap DID something
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
            return self._handle_stuck(milestone, stuck, None, observation, scoped_history)

        prev_action = (
            milestone_history[-1].action_decision.action
            if milestone_history and milestone_history[-1].action_decision
            else None
        )

        # 连续调值类（picker/步进器收敛）：重复同一列滚动是正常的，但动作区连续不动不是正常进展。
        # 因此保留 SimStuck 的「全局+动作局部均无变化」判据；只有指令重复类 RepStuck 被抑制。
        # ValueStall 作为语义兜底：当画面在动但 checker 读到的当前值长期不变，也判停滞。
        if milestone.is_iterative:
            sim_stuck = None if self._monitor.url_changed else self._monitor.check_screen_similarity(observation, self._monitor.action_center(prev_action))
            if sim_stuck is not None:
                print(f"  [Stuck] {sim_stuck.status}: {sim_stuck.reason}")
                return self._handle_stuck(
                    milestone, sim_stuck, check.read_instruction, observation, scoped_history,
                    page_changed=False,
                    prev_page_id=prev_page_id, current_page_id=current_page_id,
                )
            self._last_check_summary[milestone.id] = check.summary
            stall = self._monitor.check_value_stall(milestone, check)
            if stall is not None:
                return self._handle_stuck(
                    milestone, stall, check.read_instruction, observation, scoped_history,
                    page_changed=False,
                    prev_page_id=prev_page_id, current_page_id=current_page_id,
                )
            return self._plan_single(milestone, check, observation, scoped_history)

        sim_stuck = None if (self._monitor.url_changed or self._monitor.dom_changed) else self._monitor.check_screen_similarity(observation, self._monitor.action_center(prev_action))
        self._last_check_summary[milestone.id] = check.summary

        has_dom_state = bool(getattr(observation, "dom_state", None))
        rep_stuck = (
            None
            if sim_stuck or has_dom_state
            else self._monitor.check_instruction_repetition(scoped_history, milestone.id)
        )
        # Stepping a picker / value means repeating the SAME column scroll. The two-tier
        # _check_screen_similarity already returns stuck when the touched region is NOT moving,
        # so reaching here (sim_stuck is None) means the region IS changing — a repeated
        # value-adjust scroll is progress, not a loop. Don't let repetition flag it.
        if rep_stuck is not None and self._monitor.is_value_adjust(prev_action):
            print("  [RepStuck] 已抑制：调值类重复滚动属正常（动作区在变）")
            rep_stuck = None
        # Same exemption from ground truth: the interactive-state fingerprint changed since
        # last turn, so the "similar" instructions are advancing through a form, not looping.
        if rep_stuck is not None and self._monitor.dom_changed:
            print("  [RepStuck] 已抑制：交互状态指纹在变化（填表推进中）")
            rep_stuck = None
        if sim_stuck or rep_stuck:
            stuck = sim_stuck or rep_stuck
            assert stuck is not None
            print(f"  [Stuck] {stuck.status}: {stuck.reason}")
            page_changed = sim_stuck is None
            return self._handle_stuck(
                milestone, stuck, check.read_instruction, observation, scoped_history,
                page_changed=page_changed,
                prev_page_id=prev_page_id,
                current_page_id=current_page_id,
            )
        return self._plan_single(milestone, check, observation, scoped_history)

    def _plan_single(
        self,
        milestone: Milestone,
        check: _SingleCheckResult,
        observation: Observation,
        history: list[PolicyTurn],
    ) -> SupervisorStep:
        execution_scope = self._execution_scope_for(milestone, observation)
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
        #
        # Browser/DOM-backed pages use executed action signatures instead of planner text:
        # before action_policy runs we do not yet know which DOM element will be targeted. Using
        # natural-language similarity here falsely collapses foreach rows such as "打开 SKU A Edit"
        # and "打开 SKU B Edit". The actual key is recorded after executor DOM snap in
        # note_executed_action().
        _has_dom_state = bool(getattr(observation, "dom_state", None))
        if (
            not milestone.is_iterative
            and not _has_dom_state
            and self._is_repeated_instruction(plan.instruction, milestone.id, history)
        ):
            if self._monitor.dom_changed:
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
        # Action-Loop Guard (task-level repeat catch): the frame-level guards are defeated by a
        # Reset→search→Reset oscillation (every turn changes url/DOM → looks like progress). Key on
        # the CANONICAL url instead: if this exact (state, action) was already done earlier, the
        # agent is looping, not advancing — force a NEW action via the stuck path (bounded by the
        # replanner's retries). Browser-only (visual platforms have no url → skip).
        _state = canonical_url(getattr(observation, "url", None))
        # Signature catch (Kind-2): re-doing the SAME concrete input — re-typing the same value into
        # the same box — which the instruction guard below misses when the planner rewords it
        # ("输入X" → "删除后输入X" → "覆盖输入X"). Scoped to `type` (its value rides the signature, so a
        # re-click of Search/Reset carries no text and is left to the instruction guard). Keyed on the
        # signature ALONE over this milestone's history, NOT on canonical_url: a filter/search/reset
        # cycle rewrites the url's path shape, so the url-keyed check_loop missed the re-types entirely
        # (regression 20260622_205544: the same long search value was typed 3×, guard never fired).
        _sig = (
            None
            if milestone.is_iterative
            else self._monitor.check_action_repetition(
                history,
                milestone.id,
                execution_scope=execution_scope,
            )
        )
        if _sig is not None:
            if _plan_is_input_like(plan.instruction) and _type_repeat_matches_current_plan(_sig, plan.instruction):
                print(f"  [LoopGuard] 重复执行了同一输入动作（{_sig}）→ 打转，强制换新")
                _con = ("⚠️ 已经把同样的内容输入过同一个输入框且没带来进展(在打转)。"
                        "必须改用截图中其他可见入口/动作，禁止再把同样内容输进同一个框。")
                if _con not in self._global_constraints:
                    self._global_constraints.append(_con)
                return self._handle_stuck(milestone, check, check.read_instruction, observation, history)
            print(
                f"  [LoopGuard] 历史中有重复输入（{_sig}），"
                "但当前计划不是同值重复输入，放行"
            )
        _interaction_state = getattr(observation, "dom_state", None) or ""
        _hit = (
            None
            if milestone.is_iterative or _interaction_state
            else self._monitor.check_loop(
                len(history) + 1,
                _state,
                plan.instruction,
                _interaction_state,
                execution_scope,
            )
        )
        if _hit is not None:
            print(f"  [LoopGuard] 同页面(canonical={_state})重复了 T{_hit.index} 做过的同一动作 → 打转，强制换新")
            _con = (f"⚠️ 在当前页面已经做过「{plan.instruction}」且没带来进展(在打转)。"
                    "必须换一个【没在该页面试过】的新动作或新入口，禁止再重复该操作。")
            if _con not in self._global_constraints:
                self._global_constraints.append(_con)
            return self._handle_stuck(milestone, check, check.read_instruction, observation, history)
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
            execution_scope=execution_scope,
            direction=plan.direction,
            drag_column=getattr(plan, "drag_column", None),
            drag_steps=drag_steps,
            is_home_screen=_is_home_identity(check.page_identity, self._prompts.home_identity_markers),
            **_ctx(milestone, check.read_instruction),
        )

    # ── Loop machine ──────────────────────────────────────────────────

    def _run_loop_turn(
        self,
        milestone: Milestone,
        observation: Observation,
        history: list[PolicyTurn],
    ) -> SupervisorStep:
        scoped_history = self._history_for_scope(history, milestone, observation)
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
            if not _has_successful_scroll_for(scoped_history, milestone.id):
                stuck = _SingleCheckResult(
                    status="stuck",
                    reason="滚动预算耗尽，但尚未观测到任何成功执行的纵向滚动",
                    stuck_reason="无法区分页面边界与无效滚动，且缺少有效滚动证据",
                    issues=["没有成功滚动记录"],
                    summary="滚动未取得可验证进展",
                )
                read_inst = None if self.task_type == "action" else _default_read_instruction(milestone)
                return self._handle_stuck(milestone, stuck, read_inst, observation, scoped_history)
            return self._advance(milestone, observation, history)

        sim_stuck = self._monitor.check_screen_similarity(observation)
        last_read_added = bool(
            scoped_history
            and scoped_history[-1].supervisor.milestone_id == milestone.id
            and scoped_history[-1].read_added_content
        )
        if sim_stuck:
            if sim_stuck.frozen:
                if not _has_successful_scroll_for(scoped_history, milestone.id):
                    print("  [Loop] 屏幕冻结且无成功滚动证据 → 判为无效滚动，触发重规划")
                    read_inst = None if self.task_type == "action" else _default_read_instruction(milestone)
                    return self._handle_stuck(milestone, sim_stuck, read_inst, observation, scoped_history)
                print("  [Loop] 屏幕冻结（≥99%），即使 reader 返回新内容也结束收集")
                return self._advance(milestone, observation, history)
            if not last_read_added:
                if not _has_successful_scroll_for(scoped_history, milestone.id):
                    print("  [Loop] 截图连续无变化且无成功滚动证据 → 判为无效滚动，触发重规划")
                    read_inst = None if self.task_type == "action" else _default_read_instruction(milestone)
                    return self._handle_stuck(milestone, sim_stuck, read_inst, observation, scoped_history)
                print("  [Loop] 截图连续无变化且无新增内容 → 判为边界，结束收集")
                return self._advance(milestone, observation, history)
            print("  [Loop] 截图相似但上一轮读到了新内容，继续收集")

        with _Timer(self._timings, self._timings_order, "loop_check", self._token_usage):
            frame = self._loop_check(milestone, observation, scoped_history)
        self._last_check = None  # loop milestones use _LoopFrameResult, not _SingleCheckResult
        print(f"  [LoopFrame] boundary={frame.boundary_reached}, should_stop={frame.should_stop}")

        # 采集启动阶段（首次成功滚动之前）拦截 loading 帧：刚应用筛选/进入列表时，页面常还在
        # 「加载中」、列表体仍显示旧内容（如范围外的更晚日期）。若读入会污染 content_notes 并把
        # stitch 基线钉在旧帧上，导致后续真实内容拼接错位、漏采中段（实测 20260607_105731：
        # 读了加载中的旧帧 → 漏掉 5/24-28、混入 5/29）。返回 is_loading 等待帧，由 runner 短路
        # 跳过本帧（不读、不喂 stitch、不计轮数）。一旦开始滚动，内容已确认渲染，不再判 loading。
        if frame.loading and not _has_successful_scroll_for(scoped_history, milestone.id):
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
            if _has_collected(scoped_history, milestone.id):
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
            if not _has_successful_scroll_for(scoped_history, milestone.id):
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
                return self._handle_stuck(milestone, stuck, read_inst, observation, scoped_history)

        if frame.boundary_reached and _last_scroll_was_for(scoped_history, milestone.id):
            print("  [Loop] 确认列表边界 → 结束收集")
            return self._advance(milestone, observation, history)

        milestone.status = "running"
        loop_summary_prefix = "继续滚动查找目标" if self.task_type == "action" else "继续滚动收集内容"
        if _last_scroll_was_for(scoped_history, milestone.id):
            return SupervisorStep(
                should_act=True,
                instruction="继续滚动",
                preformed_action=scoped_history[-1].action_decision,
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
        scoped_history = self._history_for_scope(history, milestone, observation)
        pre_existing = not any(
            t.executed for t in scoped_history
            if t.supervisor.milestone_id == milestone.id
        )
        checkbox_pre_existing_ok = checkbox_toggle_satisfies_target(
            getattr(observation, "form_controls", None),
            getattr(observation, "semantic_tree", None),
            milestone,
        )
        if milestone.require_fresh_action and pre_existing and not checkbox_pre_existing_ok:
            print(
                f"  [FreshActionRequired] 子目标「{done_name}」当前状态看似满足，"
                "但本轮尚未执行写操作，覆盖 done → in_progress"
            )
            prior = self._last_check
            check = (prior if isinstance(prior, _SingleCheckResult) else _SingleCheckResult(
                status="in_progress",
                reason="当前状态可能是历史残留，不能证明本轮写操作已执行。",
                summary="需要执行本轮写操作。",
            ))
            check = check.model_copy(update={
                "status": "in_progress",
                "reason": (
                    "当前状态可能已匹配目标值，但该 action milestone 要求本轮产生写操作；"
                    "没有本轮执行记录时不能按 pre-existing 完成。"
                ),
                "summary": "目标状态疑似已存在，但仍需执行本轮写操作。",
            })
            self._last_check = check
            return self._plan_single(milestone, check, observation, scoped_history)
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
        self._monitor.clear_screenshots()
        print(f"  子目标「{done_name}」已完成")

        if pre_existing:
            if checkbox_pre_existing_ok:
                print(
                    f"  [PreExistingCheckbox] 子目标「{done_name}」未执行动作即判完成，"
                    "目标 checkbox/toggle 已处于要求状态"
                )
            else:
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
        # precondition entry-state gates (e.g. loop/function-body「确保在列表页」) are exempt: they
        # may already hold on frame 1, so the checker must judge first (satisfied→done, no action;
        # else→in_progress→plan the return). Skipping it leaks the branch decision to the planner.
        if next_ms.kind == "navigation" and not next_ms.precondition:
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
        self._monitor.clear_screenshots()
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

        # Early Feasibility probe: a milestone already stuck EARLY_FEASIBILITY_AT times is worth an
        # infeasibility check now, not after it burns every retry (run 20260622_171843: the data_query
        # milestone churned the Reviews grid 13 turns before Feasibility fired at give-up). Once per
        # milestone; the judge is conservative-toward-feasible, so a "feasible" verdict just continues.
        if (
            EARLY_FEASIBILITY_AT <= milestone.retry_count < MAX_RETRIES
            and milestone.id not in self._early_feasibility_probed
        ):
            self._early_feasibility_probed.add(milestone.id)
            kick = self._maybe_kickback(milestone, observation, read_inst)
            if kick:
                return kick

        if milestone.retry_count >= MAX_RETRIES:
            # Feasibility Guard: before giving up, judge if the milestone is INFEASIBLE (required control
            # absent) and, if so, kick back to the orchestrator with a re-plan directive.
            kick = self._maybe_kickback(milestone, observation, read_inst)
            if kick:
                return kick
            fallback = self._try_filter_fallback(milestone, can_degrade=True, read_inst=read_inst)
            if fallback:
                return fallback
            return self._fail(milestone, check, read_inst)

        print(f"  [Replan] 第 {milestone.retry_count} 次重试...")
        # 连续调值类停滞时的纠偏方向：继续使用该平台的连续调整动作，换一种连续策略——
        # 加大幅度 / 换调整的列或轴 / 调整顺序。不要因为一轮未生效就退化成离散点击。
        iter_extra = (
            "⚠️ 当前子目标是连续调值类（需要多次滚动/拖动/步进来逼近目标值）。停滞≠不支持该控件。"
            "必须继续使用当前平台适合该控件的连续调整动作，换一种策略：加大幅度、换调整列/轴、或调整顺序。"
            "禁止直接改成点击某个可见刻度/候选值来替代连续调整；禁止仅因本轮无明显变化就判定控件不支持调整。"
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
            # The replanner can give up EARLY (before MAX_RETRIES) by escalating — another give-up
            # path. Give the Feasibility Guard a chance here too: if the milestone is infeasible (required
            # control absent), kick back to the orchestrator with a directive instead of escalating.
            kick = self._maybe_kickback(milestone, observation, read_inst)
            if kick:
                return kick
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
            is_home_screen=(
                _is_home_identity(self._last_check.page_identity, self._prompts.home_identity_markers)
                if self._last_check else False
            ),
            **_ctx(milestone, read_inst),
        )

    def _maybe_kickback(
        self, milestone: Milestone, observation: Observation, read_inst: Optional[str],
    ) -> Optional[SupervisorStep]:
        """Feasibility Guard (goal level): at give-up time, judge whether the milestone is INFEASIBLE —
        i.e. the required UI control is ABSENT from the page's actual control inventory — vs merely
        a feasible-but-stuck action problem. If infeasible, abandon it with a re-plan DIRECTIVE for
        the orchestrator (kick back) instead of a plain failure.

        Returns None (→ proceed with normal fail) whenever feasible / inconclusive / unobservable —
        a deliberate conservative-toward-feasible default so it never steals the action-level
        replanner's feasible-but-stuck cases, and naturally no-ops on visual-only platforms (no DOM
        form_controls). Only fires here, after MAX_RETRIES, so the control observation is mature."""
        from .feasibility import compose_directive, control_presence_text, judge_feasibility

        control_text = control_presence_text(observation)
        if "无适配器可感知" in control_text:
            return None  # no DOM control inventory (visual platform) → can't confirm absence
        goal = f"{milestone.name} —— {milestone.success_condition}"
        try:
            with _Timer(self._timings, self._timings_order, "feasibility", self._token_usage):
                verdict = judge_feasibility(
                    goal, control_text, self._app_knowledge or "",
                    image=getattr(observation, "png_bytes", None),
                )
        except Exception as exc:  # noqa: BLE001 - a judge failure must never crash the run
            print(f"  [Feasibility] 判定异常（{exc}），按可行处理，走常规失败")
            return None
        if verdict.feasible:
            print(f"  [Feasibility] 判定可行（{verdict.reason}）→ 不踢回，走常规处理")
            return None
        print(f"  [Feasibility] milestone 不可行 → 踢回编排器重规划。{verdict.reason}")
        milestone.status = "failed"
        return SupervisorStep(
            should_act=False,
            stop=True,  # Stage 2: stops the run carrying the directive; Stage 3 makes the loop re-decompose
            stop_reason=f"milestone 不可行，需重规划：{verdict.reason}",
            goal_completed=False,
            summary=verdict.reason,
            replan_directive=compose_directive(verdict) or None,
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
        self._monitor.clear_screenshots()
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
        # 连续调值类不记「禁止重复此指令」：会把可靠的连续调整动作拉黑，逼系统退化成离散点击。
        # 停滞时的纠偏靠 replan 换连续策略（换列/轴、加大步长、调顺序），而非禁操作。
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

    def note_collection_progress(self, text: str, *, done: bool = False) -> None:
        """Push authoritative list traversal state into checker/planner prompts."""
        self._collection_progress = text or ""
        self._collection_done = bool(done)

    def _last_action_effect_text(self, history: list[PolicyTurn]) -> str:
        """Deterministic "did the last action execute / change the page" fact for the checker.

        Task-63复盘核心: "动作是否执行成功"与"动作效果是否达成"是两个独立判断——不能用效果
        (如某列是否出现)反推动作有没有执行。这里只报告动作执行的确定性事实(URL/交互指纹
        是否变化),让 checker 据此区分"动作执行了但效果未达" vs "动作没执行",不再把效果未达
        当成"没点中"而引导无效 retry。无确定性信号(视觉平台无 DOM 指纹)时返回 ""。"""
        if not history:
            return ""
        last = history[-1]
        instr = ((last.supervisor.instruction if last.supervisor else "") or "").strip().replace("\n", " ")
        instr_brief = instr[:80]
        executed = bool(getattr(last, "executed", True))
        no_effect = bool(getattr(last, "no_effect", False))
        if not executed:
            fact = "上一步动作未被执行(动作未发出)。"
        elif self._monitor.url_changed:
            fact = "页面 URL 已变化——上一步动作确定性地产生了导航效果(动作已执行成功)。"
        elif self._monitor.dom_changed:
            fact = ("页面交互状态指纹已变化(dom_changed)——上一步动作确定性地改变了表单/焦点等"
                    "交互状态,即动作本身已成功执行(不是没点中)。")
        elif no_effect:
            fact = "动作已发出,但 settle 全程页面零变化(no_effect)——这一击对当前页面无任何效果。"
        else:
            return ""  # executed 但无 url/dom/no_effect 信号 → 无确定性事实,不注入
        return (
            "## 上一步动作的确定性执行结果(运行时事实,非视觉推断)\n"
            f"上一步动作「{instr_brief}」:{fact}\n"
            "⚠️ 判断要点: '动作是否执行成功'与'动作效果是否达成'是两个独立判断。上面的信号只"
            "说明动作是否执行/是否改变了界面状态,**不直接等于验收效果**。若动作已执行"
            "(dom_changed/URL 变化)但验收目标(如主网格出现某列、某值已设置)未达成,应判 in_progress "
            "并指出'动作已执行但效果未现、需换方式(换控件/滚动/换路径)',**不得**归因为"
            "'动作没点中/需重复点击同一控件'——重复一个已执行的动作不会产生新效果,只会打转。"
        )

    def _single_check(
        self,
        milestone: Milestone,
        observation: Observation,
        history: list[PolicyTurn],
        extra: str = "",
        execution_scope: str = "",
        effect_history: Optional[list[PolicyTurn]] = None,
    ) -> _SingleCheckResult:
        app_name = self._app_name
        if not app_name:
            for t in reversed(history):
                if t.supervisor and t.supervisor.app_name:
                    app_name = t.supervisor.app_name
                    break
        if milestone.completion_strategy == "react_until_collected" and self._collection_progress:
            extra = f"{extra}\n{self._collection_progress}".strip()
        identity_hint = _target_identity_hint(milestone, observation)
        if identity_hint:
            extra = f"{extra}\n{identity_hint}".strip()
        return run_checker(
            milestone, observation, history,
            app_name=app_name,
            task_type=self.task_type,
            constraints=self._global_constraints,
            extra=extra,
            prompts=self._prompts,
            check_knowledge=self._check_knowledge,
            context_reports=self._context_reports,
            state_trace_text=self._monitor.render(scope=execution_scope),
            last_action_effect=self._last_action_effect_text(effect_history or history),
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
            context_reports=self._context_reports,
        )

    def _select_sections(self, milestone: Milestone, check: _SingleCheckResult) -> list[str]:
        """Resolve which knowledge sections to inject this turn, via the KnowledgeSelector.

        Cache key = (milestone id, normalized page_identity): selection only changes when the
        page or the milestone changes, so most turns reuse the cached stems and cost nothing.
        Two rules keep knowledge from going permanently dark when page identity is the weak
        signal (was: an empty pick under an unidentified page got cached and disabled knowledge
        for the rest of the milestone):
          - Never cache under an UNKNOWN page (empty / 未知 / 未识别) — those turns re-decide
            every time instead of locking in an empty.
          - When the selector cleanly returns nothing, fall back to a deterministic match of
            (page identity + milestone name + success_condition) against section titles and
            selector_when lines BEFORE giving up.
        Only a KNOWN page caches its result (incl. a genuinely empty one = a real "nothing here").
        On selector failure nothing is cached either — the deterministic fallback covers the turn
        and the next turn retries the LLM."""
        if self._pk is None:
            return []
        page_id = check.page_identity or ""
        page_known = _page_known(page_id)
        key = (milestone.id, _norm_page(page_id))
        if page_known and key in self._selector_cache:
            stems = self._selector_cache[key]
            self._record_selector_report(
                milestone=milestone,
                page_identity=page_id,
                page_known=page_known,
                cache="hit",
                sections=stems,
                cached=True,
            )
            return stems
        signals = [page_id, milestone.name, milestone.success_condition]
        try:
            with _Timer(self._timings, self._timings_order, "selector", self._token_usage):
                sel = run_selector(
                    self._goal, milestone, page_id,
                    self._pk.selector_manifest(),
                    prompts=self._prompts,
                    context_reports=self._context_reports,
                )
            stems = self._pk.by_ids(sel.section_ids)
            fallback_triggered = False
            fallback_reason = ""
            if not stems:  # clean-empty selector: try the deterministic fallback before giving up
                fallback = self._pk.match_signals(signals)
                fallback_triggered = bool(fallback)
                fallback_reason = "empty_selector" if fallback else ""
                stems = fallback
            if stems or sel.section_ids:
                names = "、".join(stems) if stems else "（ID 未命中）"
                print(f"  [Selector] {names}" + (f" — {sel.reason}" if sel.reason else ""))
            if page_known:
                self._selector_cache[key] = stems
            self._record_selector_report(
                milestone=milestone,
                page_identity=page_id,
                page_known=page_known,
                cache="miss",
                section_ids=list(sel.section_ids or []),
                sections=stems,
                fallback_triggered=fallback_triggered,
                fallback_reason=fallback_reason,
                cached=page_known,
                reason=sel.reason,
            )
            return stems
        except Exception as exc:  # noqa: BLE001 — selector must never block the planner
            print(f"  [Selector] 调用失败，回退确定性模糊匹配：{exc}")
            stems = self._pk.match_signals(signals)
            self._record_selector_report(
                milestone=milestone,
                page_identity=page_id,
                page_known=page_known,
                cache="miss",
                sections=stems,
                fallback_triggered=bool(stems),
                fallback_reason="selector_error",
                cached=False,
                error=str(exc),
            )
            return stems

    def _record_selector_report(
        self,
        *,
        milestone: Milestone,
        page_identity: str,
        page_known: bool,
        cache: str,
        section_ids: list[str] | None = None,
        sections: list[str] | None = None,
        fallback_triggered: bool = False,
        fallback_reason: str = "",
        cached: bool = False,
        reason: str = "",
        error: str = "",
    ) -> None:
        self._context_reports.append({
            "kind": "selector",
            "label": "knowledge.selector",
            "milestone_id": milestone.id,
            "page_identity": page_identity,
            "page_known": page_known,
            "cache": cache,
            "section_ids": list(section_ids or []),
            "sections": list(sections or []),
            "fallback_triggered": fallback_triggered,
            "fallback_reason": fallback_reason,
            "cached": cached,
            "reason": reason,
            "error": error,
        })

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
        if milestone.completion_strategy == "react_until_collected" and self._collection_progress:
            extra = f"{extra}\n{self._collection_progress}".strip()
        return run_planner(
            milestone, check, observation, history,
            constraints=self._global_constraints,
            extra=extra,
            app_knowledge=self._app_knowledge,
            elements_knowledge=elements,
            prompts=self._prompts,
            context_reports=self._context_reports,
        )

    def _invoke_loop_scroll(
        self,
        milestone: Milestone,
        frame: _LoopFrameResult,
        observation: Observation,
    ) -> _PlanResult:
        prompt = self._prompts.loop_scroll
        plan_schema = self._prompts.plan_result_schema or _PlanResult
        return invoke_structured(
            self._llm(),
            assemble_messages(
                prompt,
                observation,
                system_blocks=[
                    milestone_block(milestone),
                    constraints_block(self._global_constraints),
                    loop_frame_summary_block(frame.summary),
                ],
                image_resize=self._prompts.image_resize,
                label="loop_scroll",
                context_reports=self._context_reports,
            ),
            plan_schema,
            trace_sink=self._context_reports,
            trace_label="loop_scroll",
        )

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
        prompt = self._prompts.replan
        msgs = assemble_messages(
            prompt, observation,
            system_blocks=[
                milestone_block(milestone),
                replan_state_block(
                    check,
                    retry_count=milestone.retry_count,
                    failure_hints=milestone.failure_hints,
                ),
                constraints_block(self._global_constraints),
                completed_milestones_block(self._milestones.values(), current_id=milestone.id),
                history_block(history, current_milestone_id=milestone.id),
                tried_instructions_block(tried),
                extra_instruction_block(extra, source="replanner_guard"),
            ],
            human_blocks=[
                knowledge_block("app_navigation", self._app_knowledge),
                knowledge_block("page_elements", self._elements_for(milestone, check)),
                # DOM control inventory (incl. each input's authoritative `current=` value) — so the
                # replanner doesn't misdiagnose a narrow scrolled input as truncated from the screenshot.
                form_controls_block(getattr(observation, "form_controls", None)),
            ],
            image_resize=self._prompts.image_resize,
            label="replanner",
            context_reports=self._context_reports,
        )
        result = invoke_structured(
            self._llm(),
            msgs,
            _ReplanResult,
            trace_sink=self._context_reports,
            trace_label="replanner",
        )
        if self._is_sequence(result.instruction):
            print("  [Replan] 多步序列，重试...")
            result = self._invoke_replanner(
                milestone, check, observation, history,
                extra="你刚才输出了多个步骤，请只返回一个原子操作。",
            )
        return result

    def _llm(self) -> ChatOpenAI:
        return _make_llm()
