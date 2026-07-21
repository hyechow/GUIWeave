"""Agentic control owner for one interactive Statement."""

from collections.abc import Callable, Iterable
from typing import Literal, Optional

from llm.structured import StructuredOutputError

from gui_agent.core.run.statement_memory import available_event_refs, build_memory_view
from gui_agent.core.run.statement_runtime import StatementRuntimeState
from gui_agent.core.run.statement_transition import validate_evidence_references
from gui_agent.core.schemas import (
    ActionIntent,
    JournalEvent,
    JsonValue,
    Observation,
    PolicyTurn,
    StatementContract,
    StatementOutcome,
    SupervisorStep,
)
from gui_agent.core.self_learning.progressive import ProgressiveKnowledge
from gui_agent.core.vision.frame_analysis import is_loading_frame

from .action_normalization import StatementActionNormalizationMixin
from .execution_scope import execution_scope_for, history_for_scope
from .llm_runtime import StatementLLMRuntimeMixin
from .observation_view import StatementObservationView, build_observation_view
from .runtime import _Timer, _ctx, _is_home_identity
from .schemas import (
    StatementPrompts,
    _ActionDraft,
    _StatementTransitionResult,
    _TransitionAction,
)


def _declared_string_values(values: dict[str, JsonValue]) -> set[str]:
    result: set[str] = set()

    def visit(value: JsonValue) -> None:
        if isinstance(value, dict):
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, (str, int, float, bool)):
            result.add(str(value).strip().casefold())

    for value in values.values():
        visit(value)
    return {value for value in result if value}


class StatementSupervisorPolicy(
    StatementLLMRuntimeMixin,
    StatementActionNormalizationMixin,
):
    """Journal memory + current observation → one LLM semantic transition.

    Runtime validates references, contract values and positive adapter
    capabilities.  It does not choose page phases, routes or fallback actions.
    """

    name = "statement"

    def __init__(
        self,
        prompts: Optional[StatementPrompts] = None,
        *,
        surface_resolver: Callable[[Observation], str] | None = None,
        mutation_control_resolver: (
            Callable[[Observation, dict[str, object]], Iterable[dict]] | None
        ) = None,
    ) -> None:
        self._prompts = prompts or StatementPrompts.neutral()
        self._surface_resolver = surface_resolver
        self._mutation_control_resolver = mutation_control_resolver
        self._static_constraints: list[str] = []
        self._statement_rt: StatementRuntimeState | None = None
        self.task_type: Literal["action", "analysis"] = "action"
        self._app_knowledge: str | None = None
        self._check_knowledge = ""
        self._elements_knowledge: str | None = None
        self._pk: ProgressiveKnowledge | None = None
        self._initial_filters: dict[str, str] | None = None
        self._last_transition_record: dict | None = None
        self._last_sections_loaded: list[str] = []
        self._context_reports: list[dict] = []
        self._goal = ""
        self._timings: dict[str, float] = {}
        self._timings_order: list[str] = []
        self._token_usage: dict[str, dict[str, int]] = {}
        self._terminal_only = False

    @property
    def _rt(self) -> StatementRuntimeState:
        if self._statement_rt is None:
            raise RuntimeError("begin_statement(...) is required before step()")
        return self._statement_rt

    @property
    def _active_instance_id(self) -> str:
        return self._statement_rt.instance_id if self._statement_rt else ""

    @property
    def _active_statement(self) -> StatementContract | None:
        return self._statement_rt.contract if self._statement_rt else None

    def surface_id(self, observation: Observation) -> str:
        if self._surface_resolver is None:
            return ""
        try:
            return str(self._surface_resolver(observation) or "")
        except Exception:
            return ""

    def set_app_knowledge(
        self,
        text: str,
        app_name: str = "",
        elements: str = "",
        sections: Optional[dict[str, str]] = None,
        check: str = "",
    ) -> None:
        del app_name
        self._app_knowledge = text
        self._elements_knowledge = elements or None
        self._check_knowledge = check
        self._pk = ProgressiveKnowledge(sections) if sections else None

    def begin_statement(
        self,
        contract: StatementContract,
        *,
        instance_id: str,
        task_type: Literal["action", "analysis"] = "action",
    ) -> None:
        if self._statement_rt is not None:
            raise RuntimeError("end the active statement before beginning another")
        self._statement_rt = StatementRuntimeState(
            contract=contract,
            instance_id=instance_id,
            task_type=task_type,
        )
        self.task_type = task_type
        self._goal = contract.goal
        self._statement_rt.execution_scope = self._statement_rt.scope_key()

    def resume_statement(
        self,
        contract: StatementContract,
        *,
        instance_id: str,
        history: list[PolicyTurn],
    ) -> None:
        turns = [turn for turn in history if turn.statement_instance_id == instance_id]
        snapshot = next(
            (turn.runtime_state for turn in reversed(turns) if turn.runtime_state),
            None,
        )
        active = snapshot.contract if snapshot else contract
        self.begin_statement(
            active,
            instance_id=instance_id,
            task_type=snapshot.task_type if snapshot else "action",
        )
        if snapshot:
            self._rt.restore(snapshot)
            self._initial_filters = (
                dict(snapshot.initial_filters)
                if snapshot.initial_filters is not None
                else None
            )

    def end_statement(self, outcome=None) -> None:
        del outcome
        self._statement_rt = None

    def constraints_snapshot(self, scope: str | None = None) -> list[str]:
        del scope
        return list(self._static_constraints)

    def add_static_constraint(self, text: str) -> None:
        if text and text not in self._static_constraints:
            self._static_constraints.append(text)

    def _scope_for(self, statement: StatementContract, observation: Observation) -> str:
        return execution_scope_for(
            statement,
            observation,
            instance_id=self._active_instance_id,
        )

    def _enrich_observation(
        self,
        statement: StatementContract,
        observation: Observation,
    ) -> Observation:
        if self._mutation_control_resolver is None or not statement.required_values:
            return observation
        try:
            derived = list(
                self._mutation_control_resolver(observation, statement.required_values)
            )
        except Exception:
            return observation
        if not derived:
            return observation
        return observation.model_copy(
            update={"form_controls": [*(observation.form_controls or []), *derived]}
        )

    def step(
        self,
        observation: Observation,
        goal: str,
        history: list[JournalEvent],
    ) -> SupervisorStep:
        del goal
        self._timings.clear()
        self._timings_order.clear()
        self._token_usage.clear()
        self._last_sections_loaded = []
        self._context_reports = []
        self._last_transition_record = None
        if self._initial_filters is None and observation.applied_filters is not None:
            self._initial_filters = dict(observation.applied_filters)
        statement = self._active_statement
        if statement is None:
            raise RuntimeError("begin_statement(...) is required before step()")
        return self._run_single_turn(
            statement,
            self._enrich_observation(statement, observation),
            history,
        )

    def reconcile(
        self,
        observation: Observation,
        goal: str,
        history: list[JournalEvent],
    ) -> SupervisorStep:
        self._terminal_only = True
        try:
            return self.step(observation, goal, history)
        finally:
            self._terminal_only = False

    def replan_after_action_rejection(
        self,
        observation: Observation,
        goal: str,
        history: list[JournalEvent],
        rejected_step: SupervisorStep,
        rejection: str,
    ) -> SupervisorStep:
        """Feed a mechanical veto back to Transition on the same frame."""
        del goal, rejected_step
        statement = self._active_statement
        if statement is None:
            raise RuntimeError("cannot replan without an active statement")
        constraint = (
            "上一个候选动作未执行，因为 Runtime Guard 拒绝："
            f"{rejection}。保持原 statement 目标，基于当前画面选择另一可执行动作。"
        )
        self._static_constraints.append(constraint)
        try:
            return self._run_single_turn(statement, observation, history)
        finally:
            self._static_constraints.pop()

    @staticmethod
    def _proposal_plan(
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
        except Exception as exc:
            return None, f"invalid action proposal: {exc}"

    @staticmethod
    def _validate_declared_write(
        statement: StatementContract,
        plan: _ActionDraft,
    ) -> str:
        allowed = _declared_string_values(statement.required_values)
        if plan.atomic_role != "write" or not allowed:
            return ""
        value = plan.target_value.strip().casefold()
        if value not in allowed:
            return f"write value {plan.target_value!r} is outside required_values"
        return ""

    @staticmethod
    def _validate_action_capability(
        view: StatementObservationView,
        plan: _ActionDraft,
    ) -> str:
        candidates = [
            item
            for item in view.affordances
            if str(item.get("label") or "").strip().casefold()
            == plan.target_control.strip().casefold()
        ]
        if plan.target_ref:
            candidates = [
                item
                for item in view.affordances
                if plan.target_ref
                in {
                    str(item.get("ref") or ""),
                    *(str(value) for value in (item.get("ref_aliases") or [])),
                }
            ]
            if not candidates:
                return f"target_ref {plan.target_ref!r} is absent from current frame"
            if len(candidates) > 1:
                return f"target_ref {plan.target_ref!r} is ambiguous in current frame"
        if candidates and not any(
            plan.action_family in (item.get("supported_operations") or [])
            for item in candidates
        ):
            return f"target does not support operation {plan.action_family!r}"
        return ""

    def _materialize_action(
        self,
        decision: _StatementTransitionResult,
        statement: StatementContract,
        observation: Observation,
        *,
        execution_scope: str,
    ) -> tuple[SupervisorStep | None, str]:
        if decision.action is None:
            return None, "act transition did not provide an action"
        plan, rejection = self._proposal_plan(decision.action, decision)
        if plan is None:
            return None, rejection
        rejection = self._validate_declared_write(statement, plan)
        if rejection:
            return None, rejection
        rejection = self._validate_action_capability(
            build_observation_view(statement, observation, []), plan
        )
        if rejection:
            return None, rejection
        drag_steps = self._picker_drag_steps(plan)
        if drag_steps == 0 and plan.drag_column:
            return None, "picker target is already reached"
        return SupervisorStep(
            action_intent=ActionIntent(
                instruction=plan.instruction,
                role=plan.atomic_role,
                family=plan.action_family,
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
                decision.page_identity, self._prompts.home_identity_markers
            ),
            **_ctx(statement),
        ), ""

    def _record_transition(
        self,
        decision: _StatementTransitionResult,
        error: str = "",
    ) -> None:
        self._last_transition_record = {
            "proposal": decision.model_dump(mode="json", exclude_none=True),
            "validation_error": error,
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
            **_ctx(statement),
        )

    @staticmethod
    def _verification(
        decision: _StatementTransitionResult,
        memory,
    ) -> str:
        if any(item.source == "current_observation" for item in decision.evidence):
            return "confirmed"
        cited = {item.event_ref for item in decision.evidence}
        facts = [fact for fact in memory.durable_facts if fact.event_ref in cited]
        if any(
            fact.kind != "action_receipt"
            or str(fact.metadata.get("response") or "") == "observed"
            for fact in facts
        ):
            return "confirmed"
        return "accepted_unverified"

    @staticmethod
    def _outcome_evidence(decision: _StatementTransitionResult) -> list[str]:
        return [
            ":".join(
                part
                for part in (item.source, item.event_ref, item.claim)
                if part
            )
            for item in decision.evidence
        ]

    def _run_single_turn(
        self,
        statement: StatementContract,
        observation: Observation,
        history: list[JournalEvent],
        *,
        validation_retries: int = 1,
    ) -> SupervisorStep:
        if is_loading_frame(observation):
            return SupervisorStep(
                is_loading=True,
                summary="页面加载中，等待下一帧",
                **_ctx(statement, None),
            )

        execution_scope = self._scope_for(statement, observation)
        self._rt.execution_scope = execution_scope
        turn_history = [event for event in history if isinstance(event, PolicyTurn)]
        scoped_history = history_for_scope(
            turn_history,
            statement,
            observation,
            instance_id=self._active_instance_id,
        )
        memory = build_memory_view(
            instance_id=self._active_instance_id,
            contract=statement,
            history=turn_history,
            observation=observation,
        )
        view = build_observation_view(statement, observation, scoped_history)
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
                    memory_view=memory,
                    observation_view=view,
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
        if decision.kind in {"complete", "infeasible"}:
            citable_refs = set(available_event_refs(memory))
            refs = validate_evidence_references(
                decision.evidence,
                available_refs=citable_refs,
            )
            if not refs.allowed:
                return self._transition_failure(
                    statement, decision, refs.reason, execution_scope=execution_scope
                )
        if decision.kind == "complete":
            self._record_transition(decision)
            executed = any(
                turn.executed
                and turn.statement_instance_id == self._active_instance_id
                for turn in turn_history
            )
            verification = self._verification(decision, memory)
            summary = f"子目标「{statement.goal}」已完成。"
            return SupervisorStep(
                outcome=StatementOutcome.completed(
                    summary,
                    verification=verification,  # type: ignore[arg-type]
                    evidence=self._outcome_evidence(decision),
                ),
                pre_existing=not executed,
                summary=summary,
                **_ctx(statement),
            )
        if decision.kind == "infeasible":
            self._record_transition(decision)
            return SupervisorStep(
                outcome=StatementOutcome.infeasible(
                    decision.reason,
                    kickback=decision.kickback,
                    evidence=self._outcome_evidence(decision),
                ),
                summary=decision.assessment.summary,
                **_ctx(statement),
            )
        if self._terminal_only:
            return self._transition_failure(
                statement,
                decision,
                "hard-budget final frame cannot dispatch another action",
                execution_scope=execution_scope,
            )
        step, rejection = self._materialize_action(
            decision,
            statement,
            observation,
            execution_scope=execution_scope,
        )
        if step is None:
            if validation_retries > 0:
                constraint = (
                    "上一个 Transition 候选未执行，因为机械合同校验拒绝："
                    f"{rejection or 'invalid action'}。基于同一画面重新判断任务状态，"
                    "并给出另一条满足当前 affordance 与 required_values 的动作。"
                )
                self._static_constraints.append(constraint)
                try:
                    return self._run_single_turn(
                        statement,
                        observation,
                        history,
                        validation_retries=validation_retries - 1,
                    )
                finally:
                    self._static_constraints.pop()
            return self._transition_failure(
                statement,
                decision,
                rejection or "invalid action",
                execution_scope=execution_scope,
            )
        self._record_transition(decision)
        return step


__all__ = ["StatementSupervisorPolicy"]
