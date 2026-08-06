"""Agentic control owner for one interactive Statement."""

from collections.abc import Callable, Iterable
from typing import Literal, Optional

from llm.structured import StructuredOutputError

from gui_agent.core.filter_contract import (
    canonical_filter_field,
    canonical_filter_value,
    match_filter_state,
)
from gui_agent.core.run.action_signals import has_uncommitted_write
from gui_agent.core.run.collection_view import collection_candidates
from gui_agent.core.run.statement_memory import available_event_refs, build_memory_view
from gui_agent.core.run.target_evidence import (
    exact_identity_evidence,
    exact_target_evidence,
)
from gui_agent.core.run.lookup_scope import resolve_lookup_scope
from gui_agent.core.run.statement_runtime import StatementRuntimeState
from gui_agent.core.run.statement_transition import validate_evidence_references
from gui_agent.core.schemas import (
    ActionIntent,
    CollectionIntent,
    JournalEvent,
    JsonValue,
    Observation,
    PolicyTurn,
    StatementContract,
    StatementOutcome,
    StatementOutcomeEvent,
    SupervisorStep,
)
from gui_agent.core.self_learning.progressive import ProgressiveKnowledge
from gui_agent.core.vision.frame_analysis import is_loading_frame

from .action_normalization import StatementActionNormalizationMixin
from .context_projection import (
    declared_target_affordances,
    resolve_required_write_ref,
)
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

# Same-statement budget for complete→absent identity soft-rejects. After this many
# frame-level retries the statement exhausts instead of looping forever (lunch FN
# path: already on the right conversation, identity match still failing).
_IDENTITY_ABSENT_SOFT_BUDGET = 3


def _resolved_query_collection(
    statement: StatementContract,
    observation: Observation,
) -> dict[str, JsonValue] | None:
    intent = statement.interaction_intent
    if intent is None or intent.phase not in {"locate", "constrain"}:
        return None
    return resolve_lookup_scope(
        observation,
        CollectionIntent(
            phase="locate",
            entity=intent.entity,
            field=intent.field,
            fallback=intent.fallback,
            required_fields=intent.required_fields,
            coverage=intent.coverage,
        ),
    )


def _collection_intent(
    statement: StatementContract,
    phase: Literal["reach", "locate", "constrain"],
) -> CollectionIntent | None:
    intent = statement.interaction_intent
    return intent if intent is not None and intent.phase == phase else None


def _previous_statement_handoff(
    history: list[JournalEvent],
    current_instance_id: str,
) -> dict[str, str] | None:
    """Project only the nearest closed predecessor, never its turn history."""
    for event in reversed(history):
        if (
            isinstance(event, StatementOutcomeEvent)
            and event.statement_instance_id != current_instance_id
        ):
            return {
                "status": "closed",
                "statement_id": event.statement_id,
                "outcome": event.outcome.phase,
            }
    return None


def _controls(observation: Observation) -> list[dict]:
    return [
        control
        for control in (
            observation.form_control_state
            or observation.form_controls
            or []
        )
        if isinstance(control, dict)
    ]


def _control_identities(control: dict) -> set[str]:
    return {
        canonical_filter_field(control.get(key))
        for key in ("label", "name", "id", "group_field")
        if control.get(key)
    }


def _filter_controls(observation: Observation, field: str) -> list[dict]:
    target = canonical_filter_field(field)
    return [
        control
        for control in _controls(observation)
        if (
            control.get("is_filter") is True
            or control.get("query_action") in {"submit", "reset"}
        )
        and target in _control_identities(control)
    ]


def _control_values(control: dict) -> list[JsonValue]:
    return [
        canonical_filter_value(control[key])
        for key in ("value", "selected_text_primary", "selected_text")
        if control.get(key) not in (None, "")
    ]


def _query_action(
    view: StatementObservationView,
    action: Literal["submit", "reset"],
) -> dict | None:
    matches = [
        item
        for item in view.affordances
        if "activate" in (item.get("supported_operations") or [])
        and str(item.get("label") or "").strip()
        and item.get("query_action") == action
    ]
    return matches[0] if len(matches) == 1 else None


def _resolved_reach_collection(
    statement: StatementContract,
    observation: Observation,
) -> dict[str, JsonValue] | None:
    intent = _collection_intent(statement, "reach")
    if intent is None:
        return None
    return resolve_lookup_scope(
        observation,
        CollectionIntent(
            phase="locate",
            entity=intent.entity,
            required_fields=intent.required_fields,
        ),
    )


def _reach_is_structurally_complete(statement: StatementContract) -> bool:
    """Whether collection identity proves the complete declared reach state."""
    return set(statement.expected_state) <= {"entity", "fields"}


def _read_focus_target_in_view(
    statement: StatementContract,
    observation: Observation,
) -> bool:
    """Deterministic short-circuit for a read's navigation (focus) statement.

    ``ctx.read(target, ...)`` emits an Interact whose only job is to bring the
    target's source record into view before the one-shot Read binding. If that
    record is already a visible data cell, the navigation is satisfied without any
    action — completing here keeps the read one-shot instead of letting the LLM
    wander to a different surface.

    Detected structurally (no interaction intent, no expected state, immediate
    persistence, only ``inputs.target``), never by goal text. The target is matched
    against the observation's data cells (``collection_regions``), NOT form-control
    values: a form field that merely echoes the target string (e.g. a Title box
    pre-filled with a phone number) must not short-circuit the read into binding
    from the wrong frame.
    """
    if statement.interaction_intent is not None or statement.expected_state:
        return False
    if statement.persistence != "immediate":
        return False
    target = statement.inputs.get("target")
    if not isinstance(target, dict) or not target:
        return False
    values = [str(v).strip() for v in target.values() if v not in (None, "")]
    if not values:
        return False
    cell_texts = [
        text
        for region in observation.collection_regions or []
        for cell in region.cells or []
        # Skip cells whose content is truncated (list previews abbreviate long
        # text with "…"): a preview is not the full source record, so a read of
        # derived fields would fail to bind from it. Short-circuiting here would
        # prevent the focus from navigating to the full detail.
        if not any("…" in (item or "") for item in cell.texts or [])
        for text in cell.texts or []
    ]
    if not cell_texts:
        return False
    joined = "\n".join(cell_texts)
    return all(value in joined for value in values)


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
        self._pending_transition_correction = ""
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
        # Soft-rejects on target-identity absence for the active statement. Prevents
        # infinite act loops when the model keeps "complete" while evidence stays absent.
        self._identity_absent_streak = 0

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
        self._initial_filters = None
        self._pending_transition_correction = ""
        self._identity_absent_streak = 0
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
        self._pending_transition_correction = ""

    def constraints_snapshot(self, scope: str | None = None) -> list[str]:
        del scope
        return [
            *self._static_constraints,
            *([self._pending_transition_correction]
              if self._pending_transition_correction else []),
        ]

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

    def _mechanical_step(
        self,
        statement: StatementContract,
        *,
        execution_scope: str,
        summary: str,
        established: str,
        gap: str,
        reason: str,
        instruction: str,
        role: Literal["prepare", "write", "commit", "iterate"],
        family: Literal["input", "select", "activate", "navigate", "iterate"],
        target_control: str,
        expected_result: str,
        target_value: str = "",
        target_ref: str = "",
        direction: Literal["up", "down"] | None = None,
    ) -> SupervisorStep:
        action = {
            "instruction": instruction,
            "atomic_role": role,
            "action_family": family,
            "target_control": target_control,
            "target_value": target_value,
            "target_ref": target_ref,
            "expected_result": expected_result,
        }
        if direction is not None:
            action["direction"] = direction
        self._last_transition_record = {
            "proposal": {
                "assessment": {
                    "status": "in_progress",
                    "summary": summary,
                    "established_facts": [established],
                    "open_gaps": [gap],
                    "last_action_effect": "unknown",
                },
                "kind": "act",
                "reason": reason,
                "action": action,
            },
            "validation_error": "",
        }
        return SupervisorStep(
            action_intent=ActionIntent(
                instruction=instruction,
                role=role,
                family=family,
                target_control=target_control,
                target_value=target_value,
                target_ref=target_ref,
                expected_result=expected_result,
                direction=direction,
            ),
            summary=summary,
            execution_scope=execution_scope,
            **_ctx(statement),
        )

    def _structured_filter_write_step(
        self,
        statement: StatementContract,
        observation: Observation,
        *,
        execution_scope: str,
    ) -> SupervisorStep | None:
        constrain = _collection_intent(statement, "constrain")
        if constrain is None:
            return None
        for field, predicate in constrain.predicates.items():
            if predicate.operator != "eq" or len(predicate.values) != 1:
                continue
            matches = {
                str(
                    control.get("id")
                    or control.get("name")
                    or control.get("label")
                    or ""
                ): control
                for control in _filter_controls(observation, field)
            }
            if len(matches) != 1:
                continue
            control = next(iter(matches.values()))
            expected = predicate.values[0]
            if expected in _control_values(control):
                continue
            kind = str(control.get("kind") or "").casefold()
            family: Literal["input", "select"] = (
                "select"
                if "select" in kind or kind in {"combobox", "listbox"}
                else "input"
            )
            label = str(
                control.get("group_field")
                or control.get("label")
                or control.get("name")
                or field
            )
            value = str(expected)
            return self._mechanical_step(
                statement,
                execution_scope=execution_scope,
                summary="结构化筛选值与当前控件状态不一致",
                established=f"已定位筛选字段 {label!r} 的唯一结构化控件。",
                gap=f"该控件尚未设置为合同值 {value!r}。",
                reason="typed filter predicate differs from the DOM control value",
                instruction=f"将筛选字段 {label!r} 设置为 {value!r}。",
                role="prepare",
                family=family,
                target_control=label,
                target_value=value,
                target_ref=str(control.get("id") or control.get("name") or ""),
                expected_result=f"{label!r} 的结构化控件值变为 {value!r}。",
            )
        return None

    def _target_relocation_step(
        self,
        statement: StatementContract,
        observation: Observation,
        history: list[JournalEvent],
        *,
        execution_scope: str,
    ) -> SupervisorStep | None:
        previous = next((
            event for event in reversed(history)
            if isinstance(event, StatementOutcomeEvent)
            and event.statement_instance_id != self._active_instance_id
        ), None)
        target = statement.inputs.get("target")
        if previous is None or not isinstance(target, dict) or not any(
            report.get("kind") == "collection_cursor"
            and report.get("boundary") == "end"
            and report.get("direction") == "forward"
            for report in previous.outcome.context_reports
        ):
            return None
        rows = [
            row
            for output in previous.outcome.outputs.values()
            if isinstance(output, list)
            for row in output if isinstance(row, dict)
        ]
        identity = {
            key: value
            for key, value in statement.expected_state.items()
            if key not in {"entity", "fields"} and target.get(key) == value
        } or next((
            {key: value}
            for key, value in target.items()
            if sum(row.get(key) == value for row in rows) == 1
        ), {})
        traversable = [
            candidate
            for candidate in collection_candidates(observation)
            if candidate.get("projection") == "cells"
            and (candidate.get("traversal") or {}).get("type") in {"scroll", "paged"}
        ]
        if (
            not identity
            or not any(
                all(row.get(key) == value for key, value in identity.items())
                for row in rows
            )
            or len(traversable) != 1
            or exact_identity_evidence(identity, observation).get("status") != "absent"
        ):
            return None
        target_label = next(iter(identity.values()), "declared target")
        return self._mechanical_step(
            statement,
            execution_scope=execution_scope,
            summary="前序完整遍历留下了确定的目标重定位方向",
            established=(
                f"当前结构化集合尚未包含精确目标 {target_label!r}。"
            ),
            gap=f"需要将精确目标 {target_label!r} 带入当前视口。",
            reason="closed collection cursor determines target relocation direction",
            instruction=(
                f"在当前集合中向上遍历，将精确目标 {target_label!r} 带入视口。"
            ),
            role="iterate",
            family="iterate",
            target_control="current collection",
            expected_result=f"精确目标 {target_label!r} 出现在当前结构化观察中。",
            direction="up",
        )

    def _structured_filter_reset_step(
        self,
        statement: StatementContract,
        observation: Observation,
        view: StatementObservationView,
        *,
        execution_scope: str,
    ) -> SupervisorStep | None:
        constrain = _collection_intent(statement, "constrain")
        actual = observation.applied_filter_state
        if (
            constrain is None
            or actual is None
            or actual.coverage != "complete"
            or not (set(actual.predicates) - set(constrain.predicates))
        ):
            return None
        reset = _query_action(view, "reset")
        if reset is None:
            return None
        label = str(reset.get("label") or "").strip()
        return self._mechanical_step(
            statement,
            execution_scope=execution_scope,
            summary="当前集合含请求之外的活动筛选",
            established="结构化筛选状态包含额外谓词。",
            gap="额外谓词会缩小请求的结果集合。",
            reason="source query must start from exactly its declared predicates",
            instruction=f"激活 {label}，清除当前集合的活动筛选。",
            role="prepare",
            family="activate",
            target_control=label,
            target_ref=str(reset.get("ref") or ""),
            expected_result="活动筛选被清空，集合可按声明谓词重新查询。",
        )

    @staticmethod
    def _staged_input_submission(
        statement: StatementContract,
        observation: Observation,
        history: list[PolicyTurn],
        view: StatementObservationView,
    ) -> tuple[PolicyTurn, dict] | None:
        prior = next((turn for turn in reversed(history) if turn.executed), None)
        if prior is None or prior.supervisor is None:
            return None
        intent = prior.supervisor.action_intent
        if intent is None or intent.family not in {"input", "select"}:
            return None

        constrain = _collection_intent(statement, "constrain")
        if constrain is not None:
            for field, predicate in constrain.predicates.items():
                observed = [
                    value
                    for control in _filter_controls(observation, field)
                    for value in _control_values(control)
                ]
                if not all(value in observed for value in predicate.values):
                    return None
        else:
            if not intent.target_value:
                return None
            target_label = canonical_filter_field(intent.target_control)
            matched_control = next((
                control for control in _controls(observation)
                if target_label in _control_identities(control)
            ), None)
            if matched_control is None or not (
                matched_control.get("is_filter") is True
                or matched_control.get("query_action") in {"submit", "reset"}
            ):
                return None
            expected = intent.target_value.strip().casefold()
            if str(matched_control.get("value") or "").strip().casefold() != expected:
                return None
            if any(
                expected in str(value).strip().casefold()
                for value in (observation.applied_filters or {}).values()
            ):
                return None

        submit = _query_action(view, "submit")
        return (prior, submit) if submit is not None else None

    def _staged_input_submission_step(
        self,
        statement: StatementContract,
        observation: Observation,
        history: list[PolicyTurn],
        view: StatementObservationView,
        *,
        execution_scope: str,
    ) -> SupervisorStep | None:
        staged = self._staged_input_submission(statement, observation, history, view)
        if staged is None:
            return None
        prior, submit = staged
        prior_intent = prior.supervisor.action_intent
        assert prior_intent is not None
        target = str(submit.get("label") or "").strip()
        target_ref = str(submit.get("ref") or "").strip()
        instruction = (
            f"激活当前界面的 {target}，提交 {prior_intent.target_control} "
            "中已经填写的查询值。"
        )
        expected = "查询提交后，结果作用域刷新并显示与该查询一致的生效状态。"
        return self._mechanical_step(
            statement,
            execution_scope=execution_scope,
            summary="查询值已填写但尚未提交",
            established=(
                f"最近一次输入已将 {prior_intent.target_control} 设置为 "
                f"{prior_intent.target_value!r}。"
            ),
            gap="当前查询仍需通过提交控件生效。",
            reason="populated query controls are staged until submitted",
            instruction=instruction,
            role="prepare",
            family="activate",
            target_control=target,
            target_ref=target_ref,
            expected_result=expected,
        )

    @staticmethod
    def _validate_action_capability(
        view: StatementObservationView,
        plan: _ActionDraft,
    ) -> str:
        if not plan.target_ref:
            return ""
        candidates = [
            item for item in view.affordances
            if plan.target_ref in {
                str(item.get("ref") or ""),
                *(str(value) for value in item.get("ref_aliases") or []),
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
        if (
            not plan.target_ref
            and plan.atomic_role == "write"
            and plan.action_family in {"input", "select"}
        ):
            plan.target_ref = resolve_required_write_ref(
                statement,
                observation,
                target_control=plan.target_control,
                target_value=plan.target_value,
            )
        view = build_observation_view(statement, observation, [])
        declared_targets = declared_target_affordances(statement, view)
        if (
            declared_targets
            and not any(
                item.get("visibility") == "visible"
                for item in declared_targets
            )
            and any(
                item.get("visibility") == "offscreen"
                for item in declared_targets
            )
            and plan.action_family != "iterate"
        ):
            return None, (
                "declared mutation target is offscreen; "
                "the next operation must transport it into view"
            )
        rejection = self._validate_action_capability(view, plan)
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

    def _soft_reject_and_retry(
        self,
        statement: StatementContract,
        observation: Observation,
        history: list[JournalEvent],
        *,
        decision: _StatementTransitionResult | None,
        reason: str,
        execution_scope: str,
        guidance: str,
        validation_retries: int,
    ) -> SupervisorStep:
        """Give one same-frame correction, then continue on the next frame."""
        if decision is not None:
            self._record_transition(decision, reason)
        if validation_retries <= 0:
            self._pending_transition_correction = guidance
            message = f"Statement postcondition remains unsatisfied: {reason}"
            return SupervisorStep(
                retry_transition=True,
                summary=message,
                execution_scope=execution_scope,
                **_ctx(statement),
            )
        self._static_constraints.append(guidance)
        try:
            return self._run_single_turn(
                statement,
                observation,
                history,
                validation_retries=validation_retries - 1,
            )
        finally:
            self._static_constraints.pop()

    @staticmethod
    def _grounding_ref_feedback(memory, view: StatementObservationView) -> str:
        """Turn a repeated off_target into a use-the-exact-ref corrective.

        A single mis-estimated point is acceptable and must NOT trigger this; the
        failure the feedback chain must close is *repeated* off_target on the same
        control, where the LLM keeps blind-estimating the same icon and dying. When
        ≥2 consecutive off_target facts name the same control that also matches a
        visible ref-carrying affordance, return a constraint that binds the target
        via target_ref (structural snap handles the few-px estimate error) instead
        of a fresh visual guess.
        """
        off_targets = [fact for fact in memory.durable_facts if fact.kind == "off_target"]
        if len(off_targets) < 2:
            return ""
        # The instruction text carries the named control (落点偏离目标：<instruction>).
        targets = [
            str(fact.text).split("：", 1)[-1].strip().split(" → ")[0].strip()
            for fact in off_targets[-4:]
        ]
        from collections import Counter

        control, count = Counter(t for t in targets if t).most_common(1)[0]
        if not control or count < 2:
            return ""
        visible = [
            item
            for item in view.affordances
            if item.get("visibility") == "visible" and str(item.get("ref") or "").strip()
        ]
        if not visible:
            return ""
        names = ", ".join(
            f"「{item.get('label')}」 ref={item.get('ref')}" for item in visible[:8]
        )
        return (
            f"目标「{control}」已连续 {count} 次 off_target：说明对它反复做纯视觉估点"
            "无法落地。本帧必须改用结构身份精确绑定：从 affordance 里为该目标选择匹配"
            "的一个,原样填写它的 `target_ref`(目标名可写语义 label);填写 target_ref "
            "后不要再自行估点,结构 snap 会处理几 px 误差。当前可见 ref:"
            f"{names}。若目标不在这批 affordance 里,才允许照旧视觉估点。"
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
        reach_collection_scope = _resolved_reach_collection(
            statement,
            observation,
        )
        if (
            reach_collection_scope is not None
            and _reach_is_structurally_complete(statement)
        ):
            summary = f"子目标「{statement.goal}」已由当前结构化集合满足。"
            return SupervisorStep(
                outcome=StatementOutcome.completed(
                    summary,
                    verification="confirmed",
                    outputs={"scope": reach_collection_scope},
                    observation=observation,
                    observation_url=observation.url,
                ),
                pre_existing=True,
                summary=summary,
                **_ctx(statement),
            )
        lookup_scope = _resolved_query_collection(statement, observation)
        if (
            lookup_scope is not None
            and _collection_intent(statement, "locate") is not None
        ):
            summary = f"子目标「{statement.goal}」已由当前结构化观察满足。"
            return SupervisorStep(
                outcome=StatementOutcome.completed(
                    summary,
                    verification="confirmed",
                    outputs={"scope": lookup_scope},
                    observation=observation,
                    observation_url=observation.url,
                ),
                pre_existing=True,
                summary=summary,
                **_ctx(statement),
            )
        constrain_intent = _collection_intent(statement, "constrain")
        constrain_scope = (
            lookup_scope if constrain_intent is not None else None
        )
        filter_match = (
            match_filter_state(
                constrain_intent.predicates,
                observation.applied_filter_state,
            )
            if constrain_intent is not None
            else None
        )
        if (
            filter_match is not None
            and filter_match.status == "met"
            and constrain_scope is not None
        ):
            summary = f"子目标「{statement.goal}」已由当前视图筛选状态满足。"
            return SupervisorStep(
                outcome=StatementOutcome.completed(
                    summary,
                    verification="confirmed",
                    outputs={"scope": constrain_scope},
                    observation=observation,
                    observation_url=observation.url,
                ),
                pre_existing=True,
                summary=summary,
                **_ctx(statement),
            )
        if _read_focus_target_in_view(statement, observation):
            summary = (
                f"子目标「{statement.goal}」已由当前源记录满足"
                "（target 已在视口），read 可直接绑定。"
            )
            return SupervisorStep(
                outcome=StatementOutcome.completed(
                    summary,
                    verification="confirmed",
                    observation=observation,
                    observation_url=observation.url,
                ),
                pre_existing=True,
                summary=summary,
                **_ctx(statement),
            )
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
            previous_statement=_previous_statement_handoff(
                history,
                self._active_instance_id,
            ),
        )
        view = build_observation_view(statement, observation, scoped_history)
        relocation = self._target_relocation_step(
            statement,
            observation,
            history,
            execution_scope=execution_scope,
        )
        if relocation is not None:
            return relocation
        filter_reset = self._structured_filter_reset_step(
            statement,
            observation,
            view,
            execution_scope=execution_scope,
        )
        if filter_reset is not None:
            return filter_reset
        filter_write = self._structured_filter_write_step(
            statement,
            observation,
            execution_scope=execution_scope,
        )
        if filter_write is not None:
            return filter_write
        submission = self._staged_input_submission_step(
            statement,
            observation,
            scoped_history,
            view,
            execution_scope=execution_scope,
        )
        if submission is not None:
            return submission
        grounding_feedback = self._grounding_ref_feedback(memory, view)
        if grounding_feedback:
            # off_target was recorded but never turned into a corrective: re-ground
            # the repeated control via its exact ref instead of blind visual estimates.
            self._pending_transition_correction = grounding_feedback
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
                if validation_retries > 0:
                    self._static_constraints.append(
                        "上一个 Transition 输出未通过结构化合同校验："
                        f"{exc}。保持原 statement 目标，修正 kind 对应的必填字段后重新决策；"
                        "若当前仍有可执行路径，必须返回 act 而不是 failed。"
                    )
                    try:
                        return self._run_single_turn(
                            statement,
                            observation,
                            history,
                            validation_retries=validation_retries - 1,
                        )
                    finally:
                        self._static_constraints.pop()
                self._last_transition_record = {
                    "proposal": {},
                    "validation_error": message,
                }
                return SupervisorStep(
                    retry_transition=True,
                    summary=message,
                    execution_scope=execution_scope,
                    **_ctx(statement, None),
                )

        print(
            f"  [TransitionLLM] state={decision.assessment.status} "
            f"kind={decision.kind}: {decision.reason[:120]}"
        )
        if decision.kind in {"complete", "failed"}:
            citable_refs = set(available_event_refs(memory))
            refs = validate_evidence_references(
                decision.evidence,
                available_refs=citable_refs,
            )
            if not refs.allowed:
                return self._soft_reject_and_retry(
                    statement,
                    observation,
                    history,
                    decision=decision,
                    reason=refs.reason,
                    execution_scope=execution_scope,
                    guidance=(
                        "上一个终态候选引用了不存在的证据。保持当前目标，"
                        "基于本帧真实证据重新返回 act 或 complete。"
                    ),
                    validation_retries=validation_retries,
                )
        if decision.kind == "complete":
            rejection = ""
            if (
                _reach_is_structurally_complete(statement)
                and
                _collection_intent(statement, "reach") is not None
                and _resolved_reach_collection(statement, observation) is None
            ):
                rejection = (
                    "requested structural collection is not established for "
                    "the reach_collection postcondition"
                )
            lookup_scope = _resolved_query_collection(statement, observation)
            if (
                not rejection
                and _collection_intent(statement, "locate") is not None
                and lookup_scope is None
            ):
                rejection = (
                    "current observation does not resolve exactly one structural "
                    "collection for the lookup request"
                )
            constrain_intent = _collection_intent(statement, "constrain")
            constrain_scope = (
                lookup_scope if constrain_intent is not None else None
            )
            filter_match = (
                match_filter_state(
                    constrain_intent.predicates,
                    observation.applied_filter_state,
                )
                if constrain_intent is not None
                else None
            )
            if (
                not rejection
                and filter_match is not None
                and filter_match.status == "unmet"
            ):
                rejection = (
                    "requested exact filter predicate set is not established: "
                    "mismatched"
                )
            if (
                not rejection
                and constrain_intent is not None
                and constrain_scope is None
            ):
                rejection = (
                    "the filtered collection is not structurally bound on the "
                    "current observation"
                )
            # A commit's completion is verified by the durable receipt and the
            # observed values; the target-bound active-UI check belongs to
            # reach/focus (establishing the UI), not to the statement that
            # performs the durable change — after a commit the UI may
            # legitimately move on (e.g. back to a collection list).
            identity_absent = False
            if statement.persistence != "explicit_commit":
                target_evidence = exact_target_evidence(statement, observation)
                if (
                    not rejection
                    and target_evidence["status"] == "absent"
                ):
                    identity_absent = True
                    rejection = (
                        "current structured observation does not contain the exact "
                        "target identity fields: "
                        f"{target_evidence['missing_fields']!r}"
                    )
                if (
                    not rejection
                    and target_evidence.get("collection_member") is True
                ):
                    rejection = (
                        "the exact target is visible only as a member of a repeated "
                        "collection; collection membership does not establish the "
                        "target-bound active UI"
                    )
            if (
                not rejection
                and statement.persistence == "explicit_commit"
                and has_uncommitted_write(scoped_history)
            ):
                rejection = (
                    "explicit_commit requires a commit receipt after the latest "
                    "write receipt"
                )
            if rejection:
                if identity_absent:
                    self._identity_absent_streak += 1
                    if self._identity_absent_streak > _IDENTITY_ABSENT_SOFT_BUDGET:
                        if decision is not None:
                            self._record_transition(decision, rejection)
                        message = (
                            "target identity evidence stayed absent after "
                            f"{self._identity_absent_streak} complete attempts: "
                            f"{rejection}. Stop acting on this statement; the "
                            "declared identity is not observable on the current UI "
                            "or the program demanded a non-exposed identity key."
                        )
                        return SupervisorStep(
                            outcome=StatementOutcome.exhausted(message),
                            summary=message,
                            execution_scope=execution_scope,
                            **_ctx(statement),
                        )
                else:
                    self._identity_absent_streak = 0
                # A typed structural miss demotes complete → continue acting.
                return self._soft_reject_and_retry(
                    statement,
                    observation,
                    history,
                    decision=decision,
                    reason=rejection,
                    execution_scope=execution_scope,
                    guidance=(
                        "上一个 complete 候选未通过结构化合同校验："
                        f"{rejection}。不要结束 statement；继续 act 完成缺失的"
                        "合同条件，获得相应的结构化证据后再 complete。"
                        + (
                            f"（identity absent streak "
                            f"{self._identity_absent_streak}/"
                            f"{_IDENTITY_ABSENT_SOFT_BUDGET}）"
                            if identity_absent
                            else ""
                        )
                    ),
                    validation_retries=validation_retries,
                )
            self._identity_absent_streak = 0
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
                    outputs=(
                        {"scope": lookup_scope or reach_collection_scope}
                        if lookup_scope is not None
                        or reach_collection_scope is not None
                        else {}
                    ),
                    observation=observation,
                    observation_url=observation.url,
                ),
                pre_existing=not executed,
                summary=summary,
                **_ctx(statement),
            )
        if decision.kind == "failed":
            declared_path = " ".join(
                part.strip()
                for part in (decision.assessment.summary, decision.reason)
                if part.strip()
            )[:600]
            return self._soft_reject_and_retry(
                statement,
                observation,
                history,
                decision=decision,
                reason="model-declared blockage is not a terminal runtime fact",
                execution_scope=execution_scope,
                guidance=(
                    "上一个 failed 不能终止 Program。模型自己的判断是："
                    f"{declared_path}。如果这段判断已经指出返回、恢复、导航、"
                    "展开入口或继续观察等具体下一步，就证明存在可执行路径；"
                    "保持当前目标，立即把该下一步输出为 act，不要把它推给 Program。"
                ),
                validation_retries=validation_retries,
            )
        if self._terminal_only:
            reason = "hard-budget final frame cannot dispatch another action"
            self._record_transition(decision, reason)
            message = f"Statement Transition validation failed: {reason}"
            return SupervisorStep(
                outcome=StatementOutcome.exhausted(message),
                summary=message,
                execution_scope=execution_scope,
                **_ctx(statement),
            )
        step, rejection = self._materialize_action(
            decision,
            statement,
            observation,
            execution_scope=execution_scope,
        )
        if step is None:
            return self._soft_reject_and_retry(
                statement,
                observation,
                history,
                decision=decision,
                reason=rejection or "invalid action",
                execution_scope=execution_scope,
                guidance=(
                    "上一个 Transition 候选未执行，因为护栏/机械校验拒绝："
                    f"{rejection or 'invalid action'}。基于同一画面重新判断；"
                    "保持 typed interaction intent，并给出另一条可落成动作。"
                ),
                validation_retries=validation_retries,
            )
        if (
            statement.persistence == "explicit_commit"
            and has_uncommitted_write(scoped_history, commit_intent=True)
            and step.action_intent is not None
            and step.action_intent.role not in {"commit", "iterate"}
        ):
            return self._soft_reject_and_retry(
                statement,
                observation,
                history,
                decision=decision,
                reason=(
                    "a commit-intended writeback is awaiting the actual "
                    "persistence boundary"
                ),
                execution_scope=execution_scope,
                guidance=(
                    "Journal 表明最近一次 commit 意图实际只形成 write receipt，"
                    "其后尚无真正 commit。不要重开或重复写入子流程；只能通过 "
                    "iterate 定位当前表面的最终提交入口，或对该入口执行 commit。"
                ),
                validation_retries=validation_retries,
            )
        self._record_transition(decision)
        self._pending_transition_correction = ""
        return step


__all__ = ["StatementSupervisorPolicy"]
