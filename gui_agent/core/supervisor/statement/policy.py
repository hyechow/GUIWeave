"""Control-flow owner for one interactive statement."""

from collections.abc import Callable, Iterable
from typing import Literal, Optional

from gui_agent.core.vision.frame_analysis import is_loading_frame
from gui_agent.core.schemas import (
    ActionIntent,
    StatementContract,
    Observation,
    PolicyTurn,
    StatementOutcome,
    SupervisorStep,
    TargetValue,
    target_value_options,
)
from gui_agent.core.self_learning.progressive import ProgressiveKnowledge
from gui_agent.core.run.action_signals import latest_action
from .runtime import (
    _Timer,
    _ctx,
    _default_read_instruction,
    _has_collected,
    _has_successful_scroll_for,
    _is_home_identity,
)
from gui_agent.core.run.statement_runtime import StatementRuntimeState
from gui_agent.core.run.statement_memory import StatementMemoryView, build_memory_view
from gui_agent.core.run.statement_memory import available_event_refs
from gui_agent.core.run.statement_transition import (
    validate_completion,
    validate_evidence_references,
)
from .schemas import (
    StatementPrompts,
    _ActionDraft,
    _StatementTransitionResult,
    _TransitionAction,
)
from .observation_view import StatementObservationView, build_observation_view
from .action_normalization import StatementActionNormalizationMixin
from gui_agent.core.run.execution_signals import (
    ExecutionContract,
    CompletionEvaluation,
    CompletionReducer,
    claim,
)
from llm.structured import StructuredOutputError
from gui_agent.core.run.persistence import assess_persistence
from .evidence import (
    action_lifecycle_claims,
    observed_effect_signal,
    observation_state_claims,
    transition_claim,
)
from .execution_scope import (
    execution_scope_for,
    history_for_scope,
)
from .llm_runtime import StatementLLMRuntimeMixin


# ── Main class ────────────────────────────────────────────────────────


class StatementSupervisorPolicy(
    StatementLLMRuntimeMixin,
    StatementActionNormalizationMixin,
):
    """Agentic executor for one active interactive Statement.

    Journal-backed Memory and the current frame are the LLM's decision input. The LLM chooses
    the next semantic transition; deterministic components only project facts or veto invalid
    terminal/contract proposals. Program sequencing belongs to the DSL interpreter.
    """

    name = "statement"

    def __init__(
        self,
        prompts: Optional[StatementPrompts] = None,
        *,
        surface_resolver: Callable[[Observation], str] | None = None,
        mutation_control_resolver: (
            Callable[[Observation, dict[str, TargetValue]], Iterable[dict]] | None
        ) = None,
    ) -> None:
        # Platform factories inject their prompt bundle. The neutral default only supports
        # deterministic tests and tooling that do not need platform-specific visual guidance.
        self._prompts = prompts or StatementPrompts.neutral()
        self._surface_resolver = surface_resolver
        self._mutation_control_resolver = mutation_control_resolver
        self._static_constraints: list[str] = []
        self._statement_rt: StatementRuntimeState | None = None
        self.task_type: Literal["action", "analysis"] = "action"
        self._app_knowledge: Optional[str] = None
        self._check_knowledge: str = ""
        self._elements_knowledge: Optional[str] = None
        self._pk: Optional[ProgressiveKnowledge] = None  # progressive (skill-like) section loader
        self._completion_reducer = CompletionReducer()
        # Run-level filter provenance ledger: the FIRST applied-filters snapshot this run observed.
        self._initial_filters: Optional[dict[str, str]] = None
        self._last_transition_record: dict | None = None
        self._last_sections_loaded: list[str] = []
        self._context_reports: list[dict] = []
        self._goal: str = ""
        self._timings: dict[str, float] = {}
        self._timings_order: list[str] = []
        self._token_usage: dict[str, dict[str, int]] = {}
        self._terminal_only: bool = False

    def surface_id(self, observation: Observation) -> str:
        """Return the adapter-defined active surface identity, if available."""
        if self._surface_resolver is None:
            return ""
        try:
            return str(self._surface_resolver(observation) or "")
        except Exception as exc:  # noqa: BLE001 - optional structure must not block execution
            print(f"  [TargetBinding] 活动表面解析失败，退回无表面身份：{exc}")
            return ""

    def _mutation_observation(
        self,
        observation: Observation,
        desired_state: dict[str, TargetValue],
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
        # _check.md is app-specific evidence semantics (rendered aliases, success cues, error
        # meanings). The unified Transition receives it as knowledge; it is never promoted into
        # Journal facts without an observation/receipt path.
        self._check_knowledge = check
        # Per-section bodies are selected deterministically from current route/title/contract
        # signals and injected into the same Transition call.
        self._pk = ProgressiveKnowledge(sections) if sections else None




    # ── StatementRuntime accessors ────────────────────────────────────
    @property
    def _rt(self) -> StatementRuntimeState:
        if self._statement_rt is None:
            raise RuntimeError(
                "InteractiveStatementExecutor requires begin_statement(...) before step()"
            )
        return self._statement_rt

    @property
    def _active_instance_id(self) -> str:
        """Instance id of the active invocation, or "" outside a statement."""
        return self._statement_rt.instance_id if self._statement_rt is not None else ""

    @property
    def _active_statement(self):
        return None if self._statement_rt is None else self._statement_rt.contract

    def _scope_for(self, statement, observation: Observation) -> str:
        return execution_scope_for(
            statement,
            observation,
            instance_id=self._active_instance_id,
        )

    @property
    def _current_execution_scope(self) -> str:
        return "" if self._statement_rt is None else self._statement_rt.execution_scope

    @_current_execution_scope.setter
    def _current_execution_scope(self, value: str) -> None:
        if self._statement_rt is not None:
            self._statement_rt.execution_scope = value

    def begin_statement(
        self,
        contract,
        *,
        instance_id: str,
        task_type: Literal["action", "analysis"] = "action",
    ) -> None:
        """Begin one interactive statement; creates a fresh StatementRuntimeState."""
        if self._statement_rt is not None:
            raise RuntimeError(
                "begin_statement called while a statement is already active; "
                "call end_statement first (or reset_for_return_retry for a tighten)"
            )
        self._statement_rt = StatementRuntimeState(
            contract=contract,
            instance_id=instance_id,
            task_type=task_type,
        )
        self.task_type = task_type
        self._goal = contract.name
        self._statement_rt.execution_scope = self._statement_rt.scope_key()

    def resume_statement(
        self,
        contract: StatementContract,
        *,
        instance_id: str,
        history: list[PolicyTurn],
    ) -> None:
        """Rebuild an interrupted invocation from its journal turns."""
        invocation = [
            turn for turn in history if turn.statement_instance_id == instance_id
        ]
        snapshot = next(
            (
                turn.runtime_state
                for turn in reversed(invocation)
                if turn.runtime_state is not None
            ),
            None,
        )
        active_contract = snapshot.contract if snapshot is not None else contract
        self.begin_statement(
            active_contract,
            instance_id=instance_id,
            task_type=snapshot.task_type if snapshot is not None else "action",
        )
        if snapshot is None:
            return
        self._rt.restore(snapshot)
        self.task_type = snapshot.task_type
        self._goal = active_contract.name
        self._initial_filters = (
            dict(snapshot.initial_filters)
            if snapshot.initial_filters is not None
            else None
        )
    def reset_for_return_retry(self, new_contract) -> None:
        """Return-contract tighten: reuse the invocation with a recompiled contract."""
        self._rt.reset_for_return_retry(new_contract)
        self._goal = new_contract.name

    def end_statement(self, outcome=None) -> None:
        """Destroy statement-local live state after its outcome event is durable."""
        self._statement_rt = None

    def constraints_snapshot(self, scope: str | None = None) -> list[str]:
        """Return task constraints supplied by the outer runner."""
        del scope
        return list(self._static_constraints)

    def add_static_constraint(self, text: str) -> None:
        """Add task-lifetime context supplied by the runner or decomposition boundary."""
        if text and text not in self._static_constraints:
            self._static_constraints.append(text)

    def step(self, observation: Observation, goal: str, history: list[PolicyTurn]) -> SupervisorStep:
        self._timings.clear()
        self._timings_order.clear()
        self._token_usage.clear()
        self._last_sections_loaded = []  # reset; Transition retrieval fills it per frame
        self._context_reports = []
        # Report-only materialized action metadata must never leak across turns.
        self._last_transition_record = None

        # Filter provenance baseline: first observation that carries the applied-filters channel.
        # Chips present here predate any of this run's grid actions → inherited/residue candidates.
        if self._initial_filters is None:
            applied_now = getattr(observation, "applied_filters", None)
            if applied_now is not None:
                self._initial_filters = dict(applied_now)

        statement = self._active_statement
        if statement is None:
            raise RuntimeError(
                "InteractiveStatementExecutor requires begin_statement(...) before step(); "
                "compile a Program and drive statements through ProgramRuntime"
            )
        if statement.kind == "action" and statement.target_values:
            observation = self._mutation_observation(
                observation,
                statement.target_values,
            )
        invocation_history = [
            turn for turn in history
            if turn.statement_instance_id == self._active_instance_id
        ]
        result = self._run_single_turn(statement, observation, history)
        result.effect_signal = observed_effect_signal(
            statement,
            observation,
            invocation_history,
        )
        result.execution_scope = self._scope_for(statement, observation)

        return result

    def reconcile(
        self,
        observation: Observation,
        goal: str,
        history: list[PolicyTurn],
    ) -> SupervisorStep:
        """Run the hard-budget final frame without permitting another dispatch."""
        self._terminal_only = True
        try:
            return self.step(observation, goal, history)
        finally:
            self._terminal_only = False

    def replan_after_action_rejection(
        self,
        observation: Observation,
        goal: str,
        history: list[PolicyTurn],
        rejected_step: SupervisorStep,
        rejection: str,
    ) -> SupervisorStep:
        """Expose a post-Transition mechanical rejection without hiding it behind a retry."""
        statement = self._active_statement
        if statement is None:
            raise RuntimeError("cannot replan without an active statement")
        del observation, goal, history
        diagnostic = f"Transition action rejected after decision: {rejection}"
        self._last_transition_record = {
            "proposal": (
                rejected_step.action_intent.model_dump(mode="json")
                if rejected_step.action_intent is not None
                else {}
            ),
            "validation_error": diagnostic,
        }
        return SupervisorStep(
            outcome=StatementOutcome.exhausted(diagnostic),
            summary=diagnostic,
            **_ctx(statement, None),
        )

    # ── Single-step machine ───────────────────────────────────────────

    def _collection_context(
        self,
        statement: StatementContract,
        observation: Observation,
        scoped_history: list[PolicyTurn],
        *,
        execution_scope: str,
    ) -> tuple[list, str | None, list[str]]:
        """Project collection boundary facts and enforce only its hard move budget."""
        if statement.kind != "collection":
            return [], None, []

        notes: list[str] = []
        claims: list = []
        scroll_turns = [
            turn
            for turn in scoped_history
            if turn.executed
            and turn.action_decision is not None
            and turn.action_decision.action.action_type
            in {"scroll", "drag", "scroll_to_ref"}
        ]
        budget = max(0, statement.scroll_budget)
        if budget and len(scroll_turns) >= budget:
            return claims, (
                f"collection traversal budget exhausted ({len(scroll_turns)}/{budget})"
            ), notes

        viewport = observation.viewport or {}
        explicit_boundary = bool(
            viewport.get("at_scroll_end") is True
            or viewport.get("has_next_page") is False
            or viewport.get("can_scroll_more") is False
        )
        if explicit_boundary and scroll_turns:
            claims.append(claim(
                "collection.coverage",
                "complete",
                source_type="runtime.collection_boundary",
                scope=execution_scope,
                subject_scope=execution_scope,
                evidence="adapter reports collection traversal boundary after a dispatched move",
                authoritative=True,
                coverage="complete",
            ))
            notes.append("adapter 已确认成功遍历后的集合边界")
            return claims, None, notes

        move_summary = f"已成功派发 {len(scroll_turns)} 次集合遍历动作"
        if budget:
            move_summary += f"，显式预算 {budget}"
        notes.append(move_summary)
        return claims, None, notes

    def _proposal_plan(
        self,
        action: _TransitionAction,
        decision: _StatementTransitionResult,
    ) -> tuple[_ActionDraft | None, str]:
        try:
            return _ActionDraft(
                instruction=action.instruction,
                summary=decision.assessment.summary,
                atomic_role=action.atomic_role,
                action_family=action.action_family,
                target_control=action.target_control,
                target_value=action.target_value,
                target_ref=action.target_ref,
                expected_result=action.expected_result,
                direction=action.direction,
                drag_column=action.drag_column,
                drag_current_value=action.drag_current_value,
                drag_target_value=action.drag_target_value,
            ), ""
        except Exception as exc:  # structured action can still violate the plan schema
            return None, f"invalid atomic action proposal: {exc}"

    @staticmethod
    def _validate_declared_write(
        statement: StatementContract,
        plan: _ActionDraft,
    ) -> str:
        """Validate only the declared write value; field binding belongs to Transition."""
        allowed = sorted({
            str(value)
            for desired in (statement.target_values or {}).values()
            for value in target_value_options(desired)
        })
        if not allowed:
            return ""
        proposed = str(plan.target_value).strip().casefold()
        if proposed not in {value.strip().casefold() for value in allowed}:
            return (
                "write value is outside the Statement contract: "
                f"{plan.target_value!r}; allowed={allowed}"
            )
        return ""

    @staticmethod
    def _validate_action_capability(
        view: StatementObservationView,
        plan: _ActionDraft,
    ) -> str:
        """Validate an advertised target mechanically; never rewrite its operation."""
        label = plan.target_control.strip().casefold()
        candidates = [
            item for item in view.affordances
            if str(item.get("label") or "").strip().casefold() == label
        ]
        if plan.target_ref:
            matches = [
                item for item in view.affordances
                if str(item.get("ref") or "").strip() == plan.target_ref
            ]
            if len(matches) != 1:
                return (
                    f"target_ref {plan.target_ref!r} is not a unique current-frame target"
                )
            candidates = matches
        if candidates and not any(
            plan.action_family in (item.get("supported_operations") or [])
            for item in candidates
        ):
            supported = sorted({
                str(operation)
                for item in candidates
                for operation in item.get("supported_operations") or []
            })
            return (
                f"operation {plan.action_family!r} is not supported by target "
                f"{plan.target_control!r}; supported={supported}"
            )
        return ""

    def _materialize_transition_action(
        self,
        decision: _StatementTransitionResult,
        statement: StatementContract,
        observation: Observation,
        *,
        execution_scope: str,
    ) -> tuple[SupervisorStep | None, str]:
        action = decision.action
        if action is None:
            return None, f"{decision.kind} transition did not provide an action"
        plan, rejection = self._proposal_plan(action, decision)
        if plan is None:
            return None, rejection

        if plan.atomic_role == "write":
            rejection = self._validate_declared_write(statement, plan)
            if rejection:
                return None, rejection
        rejection = self._validate_action_capability(
            build_observation_view(statement, observation, []), plan
        )
        if rejection:
            return None, rejection
        atomic_role = plan.atomic_role
        action_family = plan.action_family

        drag_steps = self._picker_drag_steps(plan)
        if drag_steps == 0 and plan.drag_column:
            return None, (
                f"picker column {plan.drag_column!r} is already at its target; "
                "a different action is required"
            )

        read_instruction = decision.read_instruction
        if statement.kind in {"collection", "verification"} and not read_instruction:
            read_instruction = _default_read_instruction(statement)
        print(f"  [TransitionAction] {plan.instruction}")
        return SupervisorStep(
            action_intent=ActionIntent(
                instruction=plan.instruction,
                role=atomic_role,
                family=action_family,
                target_control=plan.target_control,
                target_value=plan.target_value,
                target_ref=plan.target_ref,
                expected_result=plan.expected_result,
                direction=plan.direction,
                drag_column=plan.drag_column,
                drag_steps=drag_steps,
            ),
            summary=decision.assessment.summary,
            execution_scope=execution_scope,
            is_home_screen=_is_home_identity(
                decision.page_identity,
                self._prompts.home_identity_markers,
            ),
            **_ctx(statement, read_instruction),
        ), None

    def _record_transition(
        self,
        decision: _StatementTransitionResult,
        validation_error: str = "",
    ) -> None:
        self._last_transition_record = {
            "proposal": decision.model_dump(mode="json", exclude_none=True),
            "validation_error": validation_error,
        }

    def _transition_failure(
        self,
        statement: StatementContract,
        decision: _StatementTransitionResult,
        reason: str,
        *,
        execution_scope: str,
    ) -> SupervisorStep:
        self._record_transition(decision, reason)
        message = f"Statement Transition validation failed: {reason}"
        return SupervisorStep(
            outcome=StatementOutcome.exhausted(message),
            summary=message,
            execution_scope=execution_scope,
            **_ctx(statement, decision.read_instruction),
        )

    def _run_single_turn(
        self,
        statement: StatementContract,
        observation: Observation,
        history: list[PolicyTurn],
    ) -> SupervisorStep:
        """Memory + current frame → Act | Complete | Infeasible.

        A running result always contains an executable action. Loading is detected before the
        model call. Invalid proposals fail visibly; Runtime never chooses or repairs a route.
        """
        if is_loading_frame(observation):
            return SupervisorStep(
                is_loading=True,
                summary="页面加载中（确定性空白帧），等待...",
                **_ctx(statement, None),
            )

        execution_scope = self._scope_for(statement, observation)
        self._current_execution_scope = execution_scope
        invocation_history = [
            turn for turn in history
            if turn.statement_instance_id == self._active_instance_id
        ]
        scoped_history = history_for_scope(
            history,
            statement,
            observation,
            instance_id=self._active_instance_id,
        )
        memory_view = build_memory_view(
            instance_id=self._active_instance_id or "",
            contract=statement,
            history=history,
            observation=observation,
        )

        contract = ExecutionContract.from_statement(statement)
        latest_dispatch = latest_action(invocation_history, statement.id)
        persistence_scope = (
            getattr(latest_dispatch.supervisor, "execution_scope", "")
            if latest_dispatch is not None
            else ""
        ) or execution_scope
        persistence_assessment = assess_persistence(
            statement,
            invocation_history,
            scope=persistence_scope,
            current_surface=self.surface_id(observation),
        )
        claims = action_lifecycle_claims(
            statement,
            invocation_history,
            scope=execution_scope,
        )
        claims.extend(observation_state_claims(
            statement,
            observation,
            invocation_history,
            scope=execution_scope,
        ))
        collection_claims, collection_exhausted, _notes = (
            self._collection_context(
                statement,
                observation,
                scoped_history,
                execution_scope=execution_scope,
            )
        )
        claims.extend(collection_claims)
        if collection_exhausted:
            return SupervisorStep(
                outcome=StatementOutcome.exhausted(collection_exhausted),
                summary=collection_exhausted,
                **_ctx(statement, _default_read_instruction(statement)),
            )

        observation_view = build_observation_view(
            statement,
            observation,
            invocation_history,
        )
        with _Timer(
            self._timings,
            self._timings_order,
            "transition",
            self._token_usage,
        ):
            try:
                decision = self._invoke_statement_transition(
                    statement,
                    observation,
                    scoped_history,
                    memory_view=memory_view,
                    observation_view=observation_view,
                )
            except StructuredOutputError as exc:
                message = f"Statement Transition output invalid: {exc}"
                self._last_transition_record = {
                    "proposal": {},
                    "validation_error": message,
                }
                return SupervisorStep(
                    outcome=StatementOutcome.exhausted(message),
                    summary=message,
                    execution_scope=execution_scope,
                    **_ctx(statement, None),
                )
        print(
            f"  [TransitionLLM] state={decision.assessment.status} "
            f"kind={decision.kind}: {decision.reason[:120]}"
        )

        if decision.kind == "complete":
            refs = validate_evidence_references(
                decision.evidence,
                available_refs=available_event_refs(memory_view),
            )
            if not refs.allowed:
                return self._transition_failure(
                    statement, decision, refs.reason, execution_scope=execution_scope
                )
            completion_claims = [
                *claims,
                transition_claim(decision, scope=execution_scope),
            ]
            if statement.kind == "collection":
                has_traversal = _has_successful_scroll_for(
                    scoped_history, statement.id
                )
                declared_current_boundary = statement.completion_strategy in {
                    "read_once", "visible_once"
                }
                if declared_current_boundary or has_traversal or _has_collected(
                    scoped_history, statement.id
                ):
                    completion_claims.append(claim(
                        "collection.coverage",
                        "complete",
                        source_type="transition.collection_boundary",
                        scope=execution_scope,
                        subject_scope=execution_scope,
                        evidence="; ".join(item.claim for item in decision.evidence),
                        coverage="complete",
                    ))
            proposed_evaluation = self._completion_reducer.decide(
                contract,
                completion_claims,
                scope=execution_scope,
                persistence_assessment=persistence_assessment,
            )
            verdict = validate_completion(proposed_evaluation)
            if not verdict.allowed:
                return self._transition_failure(
                    statement,
                    decision,
                    proposed_evaluation.reason or "completion evidence insufficient",
                    execution_scope=execution_scope,
                )
            self._record_transition(decision)
            return self._advance(
                statement,
                history,
                decision=proposed_evaluation,
                final_read=(
                    _ctx(
                        statement,
                        decision.read_instruction
                        or _default_read_instruction(statement),
                    )
                    if statement.kind in {"collection", "verification"}
                    and self.task_type != "action"
                    else None
                ),
            )

        if decision.kind == "infeasible":
            refs = validate_evidence_references(
                decision.evidence,
                available_refs=available_event_refs(memory_view),
            )
            if not refs.allowed:
                return self._transition_failure(
                    statement, decision, refs.reason, execution_scope=execution_scope
                )
            self._record_transition(decision)
            return SupervisorStep(
                outcome=StatementOutcome.infeasible(
                    decision.reason,
                    kickback=decision.kickback,
                ),
                summary=decision.assessment.summary,
                **_ctx(statement, decision.read_instruction),
            )

        if self._terminal_only:
                return self._transition_failure(
                    statement,
                    decision,
                    "hard-budget final frame cannot dispatch another action",
                    execution_scope=execution_scope,
                )

        step, rejection = self._materialize_transition_action(
            decision,
            statement,
            observation,
            execution_scope=execution_scope,
        )
        if step is None:
            return self._transition_failure(
                statement,
                decision,
                rejection or "invalid action",
                execution_scope=execution_scope,
            )
        self._record_transition(decision)
        return step

    def _advance(
        self,
        statement: StatementContract,
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
        done_name = statement.name
        executed_in_invocation = any(
            turn.executed
            and turn.statement_instance_id == self._active_instance_id
            and turn.supervisor.statement_id == statement.id
            for turn in history
        )
        pre_existing = not executed_in_invocation
        print(f"  子目标「{done_name}」已完成")

        if pre_existing:
            print(f"  [PreExisting] 子目标「{done_name}」未执行任何动作即判完成，目标状态在会话前已存在")

        ctx = {
            "statement_id": statement.id,
            "statement_kind": statement.kind,
            "completion_strategy": statement.completion_strategy,
            **(final_read or {}),
        }
        verification = (
            "accepted_unverified"
            if decision.completion_status == "accepted_unverified"
            else "confirmed"
        )
        summary = f"子目标「{done_name}」已完成。"
        return SupervisorStep(
            outcome=StatementOutcome.completed(summary, verification=verification),
            pre_existing=pre_existing,
            summary=summary,
            **ctx,
        )
