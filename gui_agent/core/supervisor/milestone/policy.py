"""MilestoneSupervisorPolicy: two-machine milestone supervisor."""

from collections.abc import Callable, Iterable
from typing import Literal, Optional

from gui_agent.core.vision.frame_analysis import is_loading_frame
from gui_agent.core.schemas import (
    Milestone,
    Observation,
    PolicyTurn,
    SupervisorStep,
)
from gui_agent.core.self_learning.progressive import ProgressiveKnowledge
from gui_agent.core.run.action_ledger import ActionLedger

from .acquisition import (
    TargetAcquireController,
)
from .decomposition import MilestoneDecompositionMixin
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
)
from gui_agent.core.run.progress_monitor import (
    ProgressMonitor,
    action_signature,
    canonical_url,
)
from .schemas import (
    MilestonePrompts,
    _PlanResult,
    _ReplanResult,
    _SingleCheckResult,
)
from .stuck import MilestoneStuckMixin
from gui_agent.core.run.execution_signals import (
    ConstraintLedger,
    ExecutionContract,
    CompletionEvaluation,
    CompletionEvaluator,
    claim,
)
from gui_agent.core.run.mutation import authorize_mutation, resolve_mutation
from .action_protocol import (
    action_metadata,
    is_commit_turn,
    persistence_boundary_state,
    regresses_preparation_frontier,
    record_action_outcome,
    record_action_response,
)
from .evidence import (
    action_lifecycle_claims,
    checker_claim,
    execution_contract_for,
    observation_state_claims,
)
from .execution_scope import (
    execution_scope_for,
    history_for_current_milestone,
    history_for_scope,
)
from .llm_runtime import MilestoneLLMRuntimeMixin


# ── Main class ────────────────────────────────────────────────────────


class MilestoneSupervisorPolicy(
    MilestoneLLMRuntimeMixin,
    MilestoneDecompositionMixin,
    MilestoneStuckMixin,
):
    """The sole control-flow owner for milestone execution.

    Checkers and deterministic adapters contribute evidence, planners propose one primitive,
    and executors report what crossed the UI boundary.  None of them may advance, fail, or
    suppress a milestone; those transitions are made here after the evidence is reconciled.
    """

    name = "milestone"

    def __init__(
        self,
        prompts: Optional[MilestonePrompts] = None,
        *,
        surface_resolver: Callable[[Observation], str] | None = None,
        active_target_resolver: Callable[[Observation], Iterable[str]] | None = None,
        mutation_control_resolver: (
            Callable[[Observation, dict[str, str]], Iterable[dict]] | None
        ) = None,
    ) -> None:
        # Platform factories inject their prompt bundle. The neutral default only supports
        # deterministic tests and tooling that do not need platform-specific visual guidance.
        self._prompts = prompts or MilestonePrompts.neutral()
        self._surface_resolver = surface_resolver
        self._active_target_resolver = active_target_resolver
        self._mutation_control_resolver = mutation_control_resolver
        self._static_constraints: list[str] = []
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
        # Progress memory is observational. It may trigger recovery in this policy, but it never
        # suppresses a primitive inside the action executor.
        self._monitor = ProgressMonitor()
        self._action_ledger = ActionLedger()
        self._constraint_ledger = ConstraintLedger()
        self._completion_evaluator = CompletionEvaluator()
        self._execution_contract: ExecutionContract | None = None
        self._current_execution_scope: str = ""
        self._early_feasibility_probed: set[str] = set()  # milestone ids given an early Feasibility probe
        self._last_check: Optional[_SingleCheckResult] = None
        self._collection_progress: str = ""
        self._collection_done: bool = False
        # Run-level filter provenance ledger: the FIRST applied-filters snapshot this run observed.
        # Chips already present there were inherited (candidate cross-task residue); chips that
        # appear later were set by this run's own steps — task scope, not residue. Rendered into
        # the checker/planner applied-filter block so "清除残留" can no longer destroy upstream scope.
        self._initial_filters: Optional[dict[str, str]] = None
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
        self._observe_only: bool = False
        self._target_acquire = TargetAcquireController()

    def _active_targets(self, observation: Observation) -> tuple[str, ...]:
        """Read optional platform structure without making core depend on adapter schemas."""
        if self._active_target_resolver is None:
            return ()
        try:
            return tuple(self._active_target_resolver(observation))
        except Exception as exc:  # noqa: BLE001 - optional structure must not block execution
            print(f"  [ActionFrontier] 活动目标解析失败，退回视觉证据：{exc}")
            return ()

    def surface_id(self, observation: Observation) -> str:
        """Return the adapter-defined active surface identity, if available."""
        if self._surface_resolver is None:
            return ""
        try:
            return str(self._surface_resolver(observation) or "")
        except Exception as exc:  # noqa: BLE001 - optional structure must not block execution
            print(f"  [ActionFrontier] 活动表面解析失败，退回无表面身份：{exc}")
            return ""

    def _mutation_observation(
        self,
        observation: Observation,
        desired_state: dict[str, str],
    ) -> Observation:
        if self._mutation_control_resolver is None or not desired_state:
            return observation
        try:
            derived = list(self._mutation_control_resolver(observation, desired_state))
        except Exception as exc:  # noqa: BLE001 - optional structure must not block execution
            print(f"  [Mutation] 控件能力归一化失败，退回原始观察：{exc}")
            return observation
        if not derived:
            return observation
        controls = [*(observation.form_controls or []), *derived]
        return observation.model_copy(update={"form_controls": controls})

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
            "constraints": self.constraints_snapshot(),
        }

    def set_execution_contract(self, contract: ExecutionContract) -> None:
        """Install the internal contract for the next reseeded interactive statement."""
        self._execution_contract = contract

    def _add_runtime_constraint(
        self,
        text: str,
        *,
        scope: str | None = None,
        source: str = "runtime",
    ) -> None:
        actual_scope = scope or self._current_execution_scope or "task"
        self._constraint_ledger.add(text, scope=actual_scope, source=source)

    def constraints_snapshot(self, scope: str | None = None) -> list[str]:
        """Return effective static and runtime constraints for reports and eval tooling."""
        return self._constraints_for_scope(scope)

    def add_static_constraint(self, text: str) -> None:
        """Add task-lifetime context supplied by the runner or decomposition boundary."""
        if text and text not in self._static_constraints:
            self._static_constraints.append(text)

    def _constraints_for_scope(self, scope: str | None = None) -> list[str]:
        actual_scope = scope or self._current_execution_scope
        return [
            *self._static_constraints,
            *self._constraint_ledger.visible(actual_scope),
        ]

    def _collection_completion_decision(
        self,
        milestone: Milestone,
        *,
        scope: str,
        evidence: str,
    ) -> CompletionEvaluation:
        """Evaluate a typed collection-boundary fact without inventing a business outcome."""
        return self._completion_evaluator.decide(
            execution_contract_for(milestone, self._execution_contract),
            [claim(
                "collection.coverage",
                "complete",
                source_type="runtime.collection_controller",
                scope=scope,
                subject_scope=scope,
                evidence=evidence,
                authoritative=True,
                coverage="complete",
            )],
            scope=scope,
        )

    def _advance_from_collection_controller(
        self,
        milestone: Milestone,
        observation: Observation,
        history: list[PolicyTurn],
        *,
        evidence: str,
        final_read: Optional[dict] = None,
    ) -> SupervisorStep:
        scope = execution_scope_for(milestone, observation)
        decision = self._collection_completion_decision(
            milestone,
            scope=scope,
            evidence=evidence,
        )
        if decision.status != "satisfied":
            raise RuntimeError(
                f"controller attempted completion rejected by execution contract: {decision.reason}"
            )
        return self._advance(
            milestone,
            observation,
            history,
            decision=decision,
            final_read=final_read,
        )

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
            if con not in self._constraints_for_scope(scope):
                self._add_runtime_constraint(con, scope=scope, source="loop_guard")

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

        # Filter provenance baseline: first observation that carries the applied-filters channel.
        # Chips present here predate any of this run's grid actions → inherited/residue candidates.
        if self._initial_filters is None:
            applied_now = getattr(observation, "applied_filters", None)
            if applied_now is not None:
                self._initial_filters = dict(applied_now)

        if not self._order:
            with _Timer(self._timings, self._timings_order, "decompose", self._token_usage):
                self._decompose(goal, observation)

        if self._current_id is None:
            return self._terminal_step()

        milestone = self._milestones[self._current_id]
        if milestone.kind == "action" and milestone.target_values:
            observation = self._mutation_observation(
                observation,
                milestone.target_values,
            )
        if _is_loop(milestone):
            result = self._run_loop_turn(milestone, observation, history)
        else:
            result = self._run_single_turn(milestone, observation, history)
        result_ms = self._milestones.get(result.milestone_id or "", milestone)
        result.execution_scope = execution_scope_for(result_ms, observation)

        return result

    def reconcile(
        self,
        observation: Observation,
        goal: str,
        history: list[PolicyTurn],
    ) -> SupervisorStep:
        """Run one evidence/completion pass while making planning action-less."""
        self._observe_only = True
        try:
            return self.step(observation, goal, history)
        finally:
            self._observe_only = False

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
        if (
            self._execution_contract is None
            or self._execution_contract.statement_id != milestone.id
        ):
            self._execution_contract = ExecutionContract.from_milestone(milestone)
        self._monitor.clear_screenshots()  # 唯一要清的，和 _advance 一致
        # DAG `_advance` 的 nav 跳 check 镜像：fresh_advance=刚从上一个 milestone 推进过来（同帧），
        # 此时若新 milestone 是 navigation，它「in_progress by construction」，跳过首次验收直接规划
        # 第一步导航动作——省掉交接时第 2 次 checker。幂等（重点 nav 目标无害；残留 already-done 由
        # 下一轮正常 check 兜底）。action/filter/collection 一律保留 check：重跑 action 可能双执行
        # （re-send/re-submit），collection 需要 checker 的 read_instruction。一次性，step() 消费后清。
        # precondition（入口状态归一化门，如 loop/function 体的「确保在列表页」）例外：它本就可能
        # 第一帧即满足，必须让 checker 先判（满足→done、不动作、不发 stop；未满足→in_progress→planner
        # 才规划返回动作），否则 SkipCheck 会把分支判断泄漏给 planner。
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

        execution_scope = execution_scope_for(milestone, observation)
        self._current_execution_scope = execution_scope
        scoped_history = history_for_scope(history, milestone, observation)
        milestone_history = history_for_current_milestone(history, milestone, observation)

        # Freshly entered navigation milestone (reseed fresh_advance, mirror DAG _advance):
        # skip the initial done-check and plan the first nav action directly — drops the 2nd
        # checker call on the milestone hand-off. One-shot.
        if self._skip_initial_check:
            self._skip_initial_check = False
            print("  [SkipCheck] 新进入导航子目标，跳过首次验收，直接规划")
            synthetic = _SingleCheckResult(
                status="in_progress",
                outcome_status="unverified",
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
        # A Save/navigation response commonly lands on a different URL, so the source-page turn
        # is absent from ``milestone_history`` by design. Bridge only the immediately preceding
        # action of this milestone into the response update; older rows/scopes remain isolated.
        response_history = milestone_history
        if (
            history
            and history[-1].supervisor is not None
            and history[-1].supervisor.milestone_id == milestone.id
            and history[-1].executed
        ):
            response_history = [history[-1]]
        record_action_response(
            response_history,
            milestone,
            monitor=self._monitor,
            ledger=self._action_ledger,
        )
        if self._monitor.url_changed:
            print(f"  [URLChanged] {observation.url} → 已跳页(确定性)，抑制 no_effect/sim_stuck 误判")
        if self._monitor.dom_changed and not self._monitor.url_changed:
            print("  [DOMChanged] 表单值指纹有变化，抑制 stuck/重复误判")

        contract = execution_contract_for(milestone, self._execution_contract)
        boundary = persistence_boundary_state(
            milestone,
            history,
            self._active_targets(observation),
            current_surface_id=self.surface_id(observation),
        )
        evidence_claims = action_lifecycle_claims(
            milestone,
            history,
            scope=execution_scope,
            monitor=self._monitor,
            ledger=self._action_ledger,
            boundary=boundary,
        )
        evidence_claims.extend(observation_state_claims(
            milestone,
            observation,
            history,
            scope=execution_scope,
            ledger=self._action_ledger,
        ))
        latest_dispatched = self._action_ledger.latest_dispatched(
            response_history, milestone.id
        )
        terminal_response_pending = bool(
            milestone.kind == "action"
            and self._monitor.url_changed
            and latest_dispatched is not None
            and is_commit_turn(latest_dispatched, milestone)
        )

        pre_completion = self._completion_evaluator.decide(
            contract, evidence_claims, scope=execution_scope
        )
        if pre_completion.status == "satisfied" and pre_completion.completion_status == "confirmed":
            record_action_outcome(
                response_history,
                milestone,
                pre_completion,
                ledger=self._action_ledger,
            )
            check = _SingleCheckResult(
                status="done",
                reason=pre_completion.reason,
                summary="typed evidence confirmed completion",
                outcome_status="confirmed",
            )
            print(f"  [Completion] confirmed（{pre_completion.reason}）")
            self._last_check = check
            return self._advance(
                milestone,
                observation,
                history,
                decision=pre_completion,
            )

        # Target-directed scrolling is one semantic AcquireTarget operation. Adapter structure can
        # bind it deterministically; an unresolved target falls through to the normal visual path.
        # The controller owns only scrolling: expanding, clicking, navigation and writes remain
        # ordinary milestone actions after the target enters the viewport.
        acquire = None
        if not terminal_response_pending and not self._observe_only:
            acquire = self._target_acquire.decide(
                getattr(observation, "form_controls", None),
                milestone,
                scope=execution_scope,
            )
        acquire_plan = acquire.plan if acquire is not None else None
        if acquire_plan is not None:
            check = _SingleCheckResult(
                status="in_progress",
                outcome_status="unverified",
                reason=acquire_plan.summary,
                summary=acquire_plan.summary,
                missing_evidence=["目标已唯一绑定，需先滚动到视口内再执行操作。"],
            )
            self._last_check = check
            self._last_plan = acquire_plan
            milestone.status = "running"
            print(f"  [AcquireGate] {acquire_plan.summary}")
            print(f"  [Planner] {acquire_plan.instruction}")
            return SupervisorStep(
                should_act=True,
                instruction=acquire_plan.instruction,
                stop=False,
                goal_completed=False,
                summary=acquire_plan.summary,
                execution_scope=execution_scope,
                direction=acquire_plan.direction,
                atomic_role=getattr(acquire_plan, "atomic_role", "iterate"),
                action_family=getattr(acquire_plan, "action_family", "unknown"),
                target_control=getattr(acquire_plan, "target_control", ""),
                is_home_screen=False,
                **_ctx(milestone, None),
            )
        # Optional structure may be ambiguous; in that case the normal visual path can still
        # disambiguate it. Only a uniquely-bound target that repeatedly failed to move is a
        # deterministic acquire failure.
        if acquire is not None and acquire.status == "exhausted":
            check = _SingleCheckResult(
                status="stuck",
                outcome_status="unverified",
                reason=acquire.reason,
                stuck_reason=acquire.reason,
                summary="AcquireTarget could not produce a unique progressing scroll route",
                missing_evidence=["需要重新绑定唯一目标或选择新的页内获取路径。"],
            )
            self._last_check = check
            print(f"  [AcquireTarget] {acquire.status}: {acquire.reason}")
            return self._handle_stuck(
                milestone,
                check,
                None,
                observation,
                scoped_history,
            )

        with _Timer(self._timings, self._timings_order, "checker", self._token_usage):
            check = self._single_check(
                milestone,
                observation,
                scoped_history,
                execution_scope=execution_scope,
                effect_history=response_history,
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

        evidence_claims.append(checker_claim(
            check,
            scope=execution_scope,
            subject_scope=execution_scope,
        ))
        if (
            milestone.kind == "collection"
            and milestone.completion_strategy == "read_once"
            and check.status == "done"
        ):
            # The strategy itself defines the collection boundary as the current frame. The
            # checker verifies that the requested value is readable; it does not invent coverage.
            evidence_claims.append(claim(
                "collection.coverage",
                "complete",
                source_type="runtime.read_once_boundary",
                scope=execution_scope,
                subject_scope=execution_scope,
                evidence="read_once collection boundary is the current observation",
                authoritative=True,
                coverage="complete",
            ))
        post_completion = self._completion_evaluator.decide(
            contract, evidence_claims, scope=execution_scope
        )
        record_action_outcome(
            response_history,
            milestone,
            post_completion,
            ledger=self._action_ledger,
        )
        if (
            post_completion.status == "pending"
            and "action.write.required" in post_completion.conflicts
        ):
            check = check.model_copy(update={
                "status": "in_progress",
                "outcome_status": "unverified",
                "reason": post_completion.reason,
                "summary": "execution contract requires a target write before commit",
                "missing_evidence": ["先写入声明的业务目标字段，再执行终端提交。"],
            })
            self._last_check = check
        elif (
            post_completion.status == "pending"
            and "action.commit.required" in post_completion.conflicts
        ):
            check = check.model_copy(update={
                "status": "in_progress",
                "outcome_status": "unverified",
                "reason": post_completion.reason,
                "summary": "execution contract requires terminal persistence",
                "missing_evidence": ["目标字段已就绪；执行声明的保存/提交边界。"],
            })
            self._last_check = check
        accepted_unverified = post_completion.completion_status == "accepted_unverified"
        if post_completion.status == "satisfied":
            if check.status != "done" or accepted_unverified:
                check = check.model_copy(update={
                    "status": "done",
                    "reason": post_completion.reason,
                    "summary": (
                        "completion evidence accepted dispatched terminal without outcome feedback"
                        if accepted_unverified
                        else "completion evidence confirmed"
                    ),
                    "outcome_status": (
                        "unverified" if accepted_unverified else "confirmed"
                    ),
                })
            if accepted_unverified:
                print("  [Completion] dispatch accepted without outcome feedback")
            else:
                print(f"  [Completion] confirmed（{post_completion.reason}）")
            self._last_check = check
        elif check.status == "done":
            # A model-level done is only one non-authoritative claim.  Keep executing when the
            # contract still requires a fresh dispatch or a stronger state signal.
            check = check.model_copy(update={
                "status": "in_progress",
                "reason": post_completion.reason,
                "summary": "checker done was insufficient for the execution contract",
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
                return self._advance_from_collection_controller(
                    milestone,
                    observation,
                    history,
                    evidence="collection controller reports all requested rows completed",
                    final_read=final_read,
                )
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
            return self._advance(
                milestone,
                observation,
                history,
                decision=post_completion,
                final_read=final_read,
            )

        if self._observe_only:
            milestone.status = "running"
            return SupervisorStep(
                should_act=False,
                instruction=None,
                stop=False,
                goal_completed=False,
                summary=check.reason or "最终观察未确认当前执行单元完成",
                execution_scope=execution_scope,
                **_ctx(milestone, check.read_instruction),
            )

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
                outcome_status="unverified",
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
                outcome_status="unverified",
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
            print("  [RepStuck] 已抑制：表单值指纹在变化（填表推进中）")
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
        execution_scope = execution_scope_for(milestone, observation)
        if self._observe_only:
            self._last_plan = None
            milestone.status = "running"
            return SupervisorStep(
                should_act=False,
                instruction=None,
                stop=False,
                goal_completed=False,
                summary=check.reason or "最终观察未确认当前执行单元完成",
                execution_scope=execution_scope,
                **_ctx(milestone, check.read_instruction),
            )
        active_targets = self._active_targets(observation)
        current_surface_id = self.surface_id(observation)
        mutation_required = bool(milestone.kind == "action" and milestone.target_values)
        mutation_subject = (
            resolve_mutation(
                milestone,
                observation,
                history,
                surface_id=current_surface_id,
            )
            if mutation_required
            else None
        )
        boundary = persistence_boundary_state(
            milestone,
            history,
            active_targets,
            current_surface_id=current_surface_id,
        )
        parent_commit = check.outcome_status != "contradicted" and boundary.parent_pending
        mutation_owned_plan = False
        if parent_commit:
            print("  [ActionFrontier] 子流程已返回父资源，要求绑定具体持久化控件")
            with _Timer(self._timings, self._timings_order, "planner", self._token_usage):
                plan = self._invoke_planner(
                    milestone,
                    check,
                    observation,
                    history,
                    extra=(
                        "结构化执行轨迹已确认子编辑流程返回父资源，且父资源提交边界尚未跨越。"
                        "下一步必须是当前父资源的持久化提交动作；atomic_role 填 commit，"
                        "点击保存按钮时 action_family 填 activate（禁止填 commit），"
                        "target_control 必须填写当前屏幕上"
                        "实际存在的具体控件名称。"
                        "禁止留空、禁止填写抽象的 persistence boundary，也禁止重新进入子编辑流程。"
                    ),
                )
            if not boundary.accepts_parent_plan(plan, milestone, active_targets):
                print("  [ActionFrontier] 提交提议缺少可验证的当前控件，重试")
                with _Timer(self._timings, self._timings_order, "planner", self._token_usage):
                    plan = self._invoke_planner(
                        milestone,
                        check,
                        observation,
                        history,
                        extra=(
                            "上一个提交提议没有绑定到当前活动表面中的具体控件。只返回一个当前"
                            "父资源上真实可见、可点击的持久化控件；target_control 原样填写该控件"
                            "名称，atomic_role=commit；点击控件时 action_family=activate，"
                            "禁止把事务角色重复写进 action_family。"
                            "找不到时不要猜测。"
                        ),
                    )
            if not boundary.accepts_parent_plan(plan, milestone, active_targets):
                print("  [ActionFrontier] 无法绑定具体持久化控件，保持未决且不派发动作")
                plan = _PlanResult(
                    instruction="",
                    summary="父资源提交边界待处理，但当前未绑定到可验证的具体控件。",
                    atomic_role="prepare",
                    action_family="unknown",
                )
        else:
            if mutation_subject is not None and mutation_subject.status in {
                "preparing", "writable"
            }:
                mutation_owned_plan = True
                write = mutation_subject.status == "writable"
                plan = _PlanResult(
                    instruction=(
                        f"将「{mutation_subject.next_field}」设置为"
                        f"「{mutation_subject.next_value}」"
                        if write
                        else f"取消选择「{mutation_subject.target_control}」"
                    ),
                    summary=mutation_subject.evidence,
                    atomic_role="write" if write else "prepare",
                    action_family=mutation_subject.action_family,
                    target_control=mutation_subject.target_control,
                    target_value=mutation_subject.next_value if write else "",
                )
                print(f"  [Mutation] {plan.summary}")
            else:
                with _Timer(self._timings, self._timings_order, "planner", self._token_usage):
                    plan = self._invoke_planner(milestone, check, observation, history)
        if not mutation_owned_plan and self._is_sequence(plan.instruction):
            print("  [Planner] 多步序列，重试...")
            with _Timer(self._timings, self._timings_order, "planner", self._token_usage):
                plan = self._invoke_planner(
                    milestone, check, observation, history,
                    extra="你刚才输出了多个步骤，请只返回当前屏幕上马上要做的一个操作。",
                )
        if (
            not mutation_owned_plan
            and check.outcome_status != "contradicted"
            and regresses_preparation_frontier(
                plan,
                milestone,
                history,
                current_surface_id=current_surface_id,
            )
        ):
            print(
                "  [ActionFrontier] 提议回到已完成的 preparation，"
                "拒绝回退并要求新的前向动作"
            )
            with _Timer(self._timings, self._timings_order, "planner", self._token_usage):
                plan = self._invoke_planner(
                    milestone,
                    check,
                    observation,
                    history,
                    extra=(
                        "你提议的 preparation 已经执行并获得响应，之后同一事务还有其他前向动作。"
                        "禁止重新进入该旧入口。请选择当前表面上未执行过的前向动作；没有结构证据"
                        "证明已经返回父资源时，不得自行宣称提交边界已经就绪。"
                    ),
                )
            if regresses_preparation_frontier(
                plan,
                milestone,
                history,
                current_surface_id=current_surface_id,
            ):
                print("  [ActionFrontier] 重试仍回退，保持未决且不派发动作")
                plan = _PlanResult(
                    instruction="",
                    summary="事务前沿无法确认新的安全前向动作，本轮不派发。",
                    atomic_role="prepare",
                    action_family="unknown",
                )
        atomic_role, action_family = action_metadata(plan, milestone)
        mutation_authorization = None
        if mutation_owned_plan and atomic_role == "write" and mutation_subject is not None:
            mutation_authorization = authorize_mutation(milestone, mutation_subject)
        elif mutation_required and atomic_role == "write":
            if not str(getattr(plan, "target_value", "") or ""):
                # Workflow commands are preparation, not business-value writes. Correct the
                # metadata deterministically instead of spending another model call on schema
                # repair while preserving the proposed primitive.
                plan = plan.model_copy(update={"atomic_role": "prepare"})
                atomic_role = "prepare"
                print("  [Mutation] target-less write reclassified as preparation")
            if atomic_role == "write" and mutation_authorization is None:
                print("  [Mutation] unresolved or off-contract write blocked before dispatch")
                plan = _PlanResult(
                    instruction="",
                    summary="mutation write lacks a resolved subject authorization",
                    atomic_role="prepare",
                    action_family="unknown",
                )
                atomic_role, action_family = "prepare", "unknown"
        raw_action_family = getattr(plan, "action_family", "unknown")
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

        if raw_action_family != action_family:
            print(
                "  [ActionContract] normalize "
                f"role={atomic_role}: family={raw_action_family} -> {action_family}"
            )
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
            execution_scope=execution_scope,
            atomic_role=atomic_role,
            action_family=action_family,
            target_control=getattr(plan, "target_control", ""),
            target_value=getattr(plan, "target_value", ""),
            mutation_authorization=mutation_authorization,
            requires_mutation_authorization=mutation_required and atomic_role == "write",
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
        scoped_history = history_for_scope(history, milestone, observation)
        if self._observe_only:
            with _Timer(self._timings, self._timings_order, "loop_check", self._token_usage):
                frame = self._loop_check(milestone, observation, scoped_history)
            self._last_check = None
            read_inst = (
                None
                if self.task_type == "action"
                else frame.read_instruction or _default_read_instruction(milestone)
            )
            if frame.should_stop and _has_collected(scoped_history, milestone.id):
                return self._advance_from_collection_controller(
                    milestone,
                    observation,
                    history,
                    evidence=f"collection stop condition satisfied: {frame.stop_reason}",
                    final_read=_ctx(milestone, read_inst, frame.collection_scope),
                )
            if frame.boundary_reached and _last_scroll_was_for(scoped_history, milestone.id):
                return self._advance_from_collection_controller(
                    milestone,
                    observation,
                    history,
                    evidence="collection boundary reached after a successful scroll",
                )
            milestone.status = "running"
            return SupervisorStep(
                should_act=False,
                instruction=None,
                stop=False,
                goal_completed=False,
                summary=frame.stop_reason or frame.summary or "最终观察未确认循环完成",
                read_instruction=read_inst,
                allow_read=bool(read_inst),
                milestone_id=milestone.id,
                milestone_kind=milestone.kind,
                completion_strategy=milestone.completion_strategy,
                collection_scope=frame.collection_scope,
            )
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
                    outcome_status="unverified",
                    reason="滚动预算耗尽，但尚未观测到任何成功执行的纵向滚动",
                    stuck_reason="无法区分页面边界与无效滚动，且缺少有效滚动证据",
                    issues=["没有成功滚动记录"],
                    summary="滚动未取得可验证进展",
                )
                read_inst = None if self.task_type == "action" else _default_read_instruction(milestone)
                return self._handle_stuck(milestone, stuck, read_inst, observation, scoped_history)
            return self._advance_from_collection_controller(
                milestone, observation, history,
                evidence="collection scroll budget exhausted after successful traversal",
            )

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
                return self._advance_from_collection_controller(
                    milestone, observation, history,
                    evidence="collection viewport is frozen after successful traversal",
                )
            if not last_read_added:
                if not _has_successful_scroll_for(scoped_history, milestone.id):
                    print("  [Loop] 截图连续无变化且无成功滚动证据 → 判为无效滚动，触发重规划")
                    read_inst = None if self.task_type == "action" else _default_read_instruction(milestone)
                    return self._handle_stuck(milestone, sim_stuck, read_inst, observation, scoped_history)
                print("  [Loop] 截图连续无变化且无新增内容 → 判为边界，结束收集")
                return self._advance_from_collection_controller(
                    milestone, observation, history,
                    evidence="collection reached a stable boundary with no new content",
                )
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
                return self._advance_from_collection_controller(
                    milestone, observation, history,
                    evidence=f"collection stop condition satisfied: {frame.stop_reason}",
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
                    outcome_status="unverified",
                    reason=f"停止条件已触发但尚未采集到目标内容：{frame.stop_reason}",
                    stuck_reason="停止条件触发且没有可用采集结果",
                    summary=frame.summary,
                )
                return self._handle_stuck(milestone, stuck, read_inst, observation, scoped_history)

        if frame.boundary_reached and _last_scroll_was_for(scoped_history, milestone.id):
            print("  [Loop] 确认列表边界 → 结束收集")
            return self._advance_from_collection_controller(
                milestone, observation, history,
                evidence="collection boundary reached after a successful scroll",
            )

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
        *,
        decision: CompletionEvaluation,
        final_read: Optional[dict] = None,
    ) -> SupervisorStep:
        """Commit a completion already selected by this policy."""
        if decision.status != "satisfied":
            raise ValueError(
                f"cannot advance without satisfied completion evidence: {decision.status}"
            )
        done_name = milestone.name
        scoped_history = history_for_scope(history, milestone, observation)
        executed_in_scope = any(
            t.executed for t in scoped_history
            if t.supervisor.milestone_id == milestone.id
        )
        # Source-page action -> destination-page verdict is one lifecycle transition even though
        # the execution scopes differ. Bridge only the immediately preceding on-target action
        # while URL change is fresh; this does not let an earlier foreach row satisfy a later row.
        executed_in_transition = bool(
            self._monitor.url_changed
            and history
            and history[-1].executed
            and history[-1].supervisor is not None
            and history[-1].supervisor.milestone_id == milestone.id
            and (history[-1].target_verify is None or history[-1].target_verify.on_target)
        )
        pre_existing = not (executed_in_scope or executed_in_transition)
        milestone.status = "done"
        milestone.completion_status = decision.completion_status
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
            print(f"  [PreExisting] 子目标「{done_name}」未执行任何动作即判完成，目标状态在会话前已存在")

        if self._current_id is None:
            # final_read(_ctx) 已含 milestone_id/kind/completion_strategy；显式再传会撞车
            # （TypeError: got multiple values for 'milestone_id'）。合并为一个 ctx：无
            # final_read 时只带这三个 milestone 字段，有则其同名键(同值)覆盖、并补 read_* 等。
            ctx = {
                "milestone_id": milestone.id,
                "milestone_kind": milestone.kind,
                "completion_strategy": milestone.completion_strategy,
                "completion_status": milestone.completion_status,
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
                outcome_status="unverified",
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
                    cleared = self._constraint_ledger.remove_sources(
                        self._current_execution_scope,
                        {"loop_guard", "no_effect"},
                    )
                    if cleared:
                        print(f"  [Replan] 清除 {cleared} 条旧页面操作约束")
                else:
                    print(f"  [Replan] 屏幕变化但页面未变（{current_page_id or '未知'}），计入重试次数")
        if not skip_retry:
            milestone.retry_count += 1

        self._record_failure_constraint(milestone, check, history)

        # Early Feasibility probe: a milestone already stuck EARLY_FEASIBILITY_AT times is worth an
        # infeasibility check now rather than after it burns every retry. Once per milestone; the
        # judge is conservative-toward-feasible, so a "feasible" verdict just continues.
        if (
            EARLY_FEASIBILITY_AT <= milestone.retry_count < MAX_RETRIES
            and milestone.id not in self._early_feasibility_probed
        ):
            self._early_feasibility_probed.add(milestone.id)
            kick = self._maybe_kickback(milestone, observation, read_inst, history)
            if kick:
                return kick

        if milestone.retry_count >= MAX_RETRIES:
            # Feasibility Guard: before giving up, judge if the milestone is INFEASIBLE (required control
            # absent) and, if so, kick back to the orchestrator with a re-plan directive.
            kick = self._maybe_kickback(milestone, observation, read_inst, history)
            if kick:
                return kick
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
            if path_constraint not in self._constraints_for_scope():
                self._add_runtime_constraint(path_constraint, source="replan")

        if replan.strategy == "force_complete":
            # A replanner diagnosis is a proposal, not business-state evidence. The normal
            # observation/check path may confirm completion on a later frame; this turn remains
            # pending and asks for a concrete local action instead of fabricating authority.
            print("  [Replan] force_complete 不产生完成证据，退回本地规划")
            check = check.model_copy(update={
                "status": "in_progress",
                "outcome_status": "unverified",
                "reason": "replanner requested completion without new authoritative evidence",
                "summary": "completion request deferred to the normal evidence path",
            })
            self._last_check = check
            return self._plan_single(milestone, check, observation, history)

        if replan.strategy == "escalate_human":
            # The replanner can give up EARLY (before MAX_RETRIES) by escalating — another give-up
            # path. Give the Feasibility Guard a chance here too: if the milestone is infeasible (required
            # control absent), kick back to the orchestrator with a directive instead of escalating.
            kick = self._maybe_kickback(milestone, observation, read_inst, history)
            if kick:
                return kick
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

        atomic_role, action_family = action_metadata(replan, milestone)
        mutation_required = bool(milestone.kind == "action" and milestone.target_values)
        if mutation_required and atomic_role == "write":
            print("  [Mutation] replanner write returned to the single mutation planner")
            check = check.model_copy(
                update={
                    "status": "in_progress",
                    "outcome_status": "unverified",
                    "reason": "mutation write requires normal subject resolution",
                }
            )
            self._last_check = check
            return self._plan_single(milestone, check, observation, history)
        drag_steps = self._picker_drag_steps(replan)
        milestone.status = "running"
        return SupervisorStep(
            should_act=bool(replan.instruction),
            instruction=replan.instruction or None,
            stop=False,
            goal_completed=False,
            summary=f"子目标「{milestone.name}」尚未达成，第 {milestone.retry_count} 次调整策略。{replan.diagnosis}",
            atomic_role=atomic_role,
            action_family=action_family,
            target_control=replan.target_control,
            target_value=replan.target_value,
            direction=replan.direction,
            drag_column=replan.drag_column,
            drag_steps=drag_steps,
            is_home_screen=(
                _is_home_identity(self._last_check.page_identity, self._prompts.home_identity_markers)
                if self._last_check else False
            ),
            **_ctx(milestone, read_inst),
        )

    def _maybe_kickback(
        self, milestone: Milestone, observation: Observation, read_inst: Optional[str],
        history: Optional[list[PolicyTurn]] = None,
    ) -> Optional[SupervisorStep]:
        """Feasibility Guard (goal level): at give-up time, judge whether the milestone is INFEASIBLE —
        i.e. the required UI control is ABSENT from the page's actual control inventory — vs merely
        a feasible-but-stuck action problem. If infeasible, abandon it with a re-plan DIRECTIVE for
        the orchestrator (kick back) instead of a plain failure.

        Returns None (→ proceed with normal fail) whenever feasible / inconclusive / unobservable —
        a deliberate conservative-toward-feasible default so it never steals the action-level
        replanner's feasible-but-stuck cases, and naturally no-ops on visual-only platforms (no DOM
        form_controls). Only fires here, after MAX_RETRIES, so the control observation is mature."""
        from .feasibility import (
            compose_directive,
            control_presence_text,
            judge_feasibility,
            semantic_target_present,
        )

        # Don't kick back a milestone whose terminal submit/save already dispatched successfully:
        # the write is done and the required control is "absent" only because we already used it and
        # the page redirected away. Re-decomposing here could redo the completed mutation after the
        # original form has vanished. Degrade to a clean bounded failure instead. This is skipped
        # when the frame shows negative feedback because that means the submit did not take.
        if (
            history is not None
            and self._action_ledger.latest_commit(history, milestone.id) is not None
            and not (
                isinstance(self._last_check, _SingleCheckResult)
                and self._last_check.outcome_status == "contradicted"
            )
        ):
            print("  [Feasibility] 跳过踢回：该里程碑的终端提交已成功派发，不把已完成的 mutation 送去重规划")
            return None

        # A form-control inventory cannot disprove a navigation link. When the latest structured
        # execution target is present in the AX navigation inventory, an infeasible verdict would
        # directly contradict authoritative observation. This does not mark the milestone done;
        # it only leaves the feasible-but-stuck route with the local replanner.
        if milestone.kind == "navigation":
            target_hints = list(milestone.target_controls)
            if history:
                for turn in reversed(history):
                    step = turn.supervisor
                    if step is None or step.milestone_id != milestone.id:
                        continue
                    if step.target_control:
                        target_hints.append(step.target_control)
                    break
            if semantic_target_present(
                getattr(observation, "semantic_tree", None),
                target_hints,
            ):
                print(
                    "  [Feasibility] 导航目标已在 AX 入口清单中，"
                    "表单控件清单不能推翻该直接观察 → 默认可行"
                )
                return None

        control_text = control_presence_text(observation)
        if "无适配器可感知" in control_text:
            return None  # no DOM control inventory (visual platform) → can't confirm absence
        controls_meta = getattr(observation, "form_controls_meta", None) or {}
        if controls_meta.get("coverage") == "partial":
            print("  [Feasibility] 控件清单 coverage=partial，缺失不能证明不存在 → 默认可行")
            return None
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
        if constraint not in self._constraints_for_scope():
            self._add_runtime_constraint(constraint, source="no_effect")
            print(f"  [Constraint] {constraint}")

    def note_collection_progress(self, text: str, *, done: bool = False) -> None:
        """Push authoritative list traversal state into checker/planner prompts."""
        self._collection_progress = text or ""
        self._collection_done = bool(done)

    def _last_action_effect_text(self, history: list[PolicyTurn]) -> str:
        """Deterministic execution/effect fact for the checker.

        "动作是否执行成功"与"动作效果是否达成"是两个独立判断，不能用效果
        (如某列是否出现)反推动作有没有执行。这里只报告动作执行的确定性事实(URL/交互指纹
        是否变化),让 checker 据此区分"动作执行了但效果未达" vs "动作没执行",不再把效果未达
        当成"没点中"而引导无效 retry。无确定性信号(视觉平台无 DOM 指纹)时返回 ""。"""
        if not history:
            return ""
        last = history[-1]
        instr = ((last.supervisor.instruction if last.supervisor else "") or "").strip().replace("\n", " ")
        instr_brief = instr[:80]
        signal = getattr(last, "action_signal", None)
        executed = bool(getattr(last, "executed", True))
        no_effect = bool(getattr(last, "no_effect", False))
        if signal is not None:
            execution_signal = signal.execution
            effect_signal = signal.response
            fact = (
                f"结构化动作信号：target={signal.target}, response={signal.response}, "
                f"outcome={signal.outcome}."
            )
        elif not executed:
            execution_signal = "not_dispatched"
            effect_signal = "none"
            fact = "上一步动作未被执行(动作未发出)。"
        elif self._monitor.url_changed:
            execution_signal = "dispatched"
            effect_signal = "url_changed"
            fact = "页面 URL 已变化——上一步动作确定性地产生了导航效果。"
        elif self._monitor.dom_changed:
            execution_signal = "dispatched"
            effect_signal = "dom_changed"
            fact = "页面表单值指纹已变化(dom_changed)——上一步动作确定性地改变了控件值。"
        elif no_effect:
            execution_signal = "dispatched"
            effect_signal = "no_visible_effect"
            fact = "动作已发出,但 settle 全程页面零变化(no_effect)——未观察到可见/DOM 反馈。"
        else:
            execution_signal = "dispatched"
            effect_signal = "unknown"
            fact = "动作已发出,但当前平台没有提供可判定的 URL/DOM/视觉反馈信号。"
        return (
            "## 上一步动作信号(运行时事实,非视觉推断)\n"
            f"- action: {instr_brief}\n"
            f"- execution_signal: {execution_signal}\n"
            f"- effect_signal: {effect_signal}\n"
            f"- outcome_signal: {(signal.outcome if signal is not None else 'unverified')}\n"
            f"- detail: {fact}\n"
            "裁决规则：execution_signal 只回答“动作是否已派发/执行”，effect_signal 只回答"
            "“派发后是否观察到 URL/DOM/视觉反馈”，二者都不直接等于业务目标状态。"
            "若 execution_signal=dispatched 而 effect_signal=no_visible_effect/unknown，不能写成"
            "“动作没点中/未执行/需要重复点击同一控件”；只能说“动作已执行但当前反馈通道未提供"
            "目标状态证据”。对提交/保存/发送这类终端派发动作，若页面没有错误/校验失败，"
            "不要仅因缺少成功 toast/历史新增可见反馈而要求重复提交。"
        )
