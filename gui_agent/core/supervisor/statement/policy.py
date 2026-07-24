"""Agentic control owner for one interactive Statement."""

from collections.abc import Callable, Iterable
import re
from typing import Literal, Optional

from llm.structured import StructuredOutputError

from gui_agent.core.run.statement_memory import available_event_refs, build_memory_view
from gui_agent.core.run.lookup_scope import resolve_lookup_scope
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


_QUERY_MARKERS = (
    "search", "query", "filter", "搜索", "查询", "筛选", "查找", "检索",
)
_QUERY_SUBMIT_MARKERS = (
    "search", "apply", "filter", "submit", "搜索", "应用", "筛选", "查询",
)
_MUTATING_MARKERS = (
    "save", "create", "add", "edit", "update", "delete", "remove",
    "discard", "reset", "cancel", "clear",
    "保存", "创建", "新增", "添加", "编辑", "更新", "删除", "移除",
    "丢弃", "重置", "取消", "清空",
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


def _semantic_terms(value: str) -> set[str]:
    return set(re.findall(r"[\w]+", value.casefold().replace("_", " ")))


def _action_targets(
    view: StatementObservationView,
    plan: _ActionDraft,
) -> list[dict]:
    if plan.target_ref:
        return [
            item for item in view.affordances
            if plan.target_ref in {
                str(item.get("ref") or ""),
                *(str(value) for value in item.get("ref_aliases") or []),
            }
        ]
    target = plan.target_control.strip().casefold()
    return [
        item for item in view.affordances
        if str(item.get("label") or "").strip().casefold() == target
    ]


def _resolved_lookup(
    statement: StatementContract,
    observation: Observation,
) -> dict[str, JsonValue] | None:
    request = statement.inputs.get("lookup_request")
    return resolve_lookup_scope(observation, request) if isinstance(request, dict) else None


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
        self._initial_filters = None
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
    def _validate_observed_field_write(
        statement: StatementContract,
        plan: _ActionDraft,
    ) -> str:
        if plan.action_family not in {"input", "select"}:
            return ""
        target = plan.target_control.strip().casefold()
        observed = {field.strip().casefold() for field in statement.observe_fields}
        if target in observed:
            return f"observed field {plan.target_control!r} is read-only"
        return ""

    @staticmethod
    def _validate_observed_field_visibility(
        statement: StatementContract,
        observation: Observation,
        plan: _ActionDraft,
    ) -> str:
        target = StatementSupervisorPolicy._offscreen_observed_field(
            statement, observation
        )
        if target is None:
            return ""
        field, position = target
        if plan.action_family != "iterate":
            return (
                f"observed field {field!r} is offscreen {position}; "
                "only iterate toward that field is allowed"
            )
        if plan.target_control.strip().casefold() != field.strip().casefold():
            return f"iterate must target the offscreen observed field {field!r}"
        expected_direction = "up" if position == "above" else "down"
        if plan.direction is not None and plan.direction != expected_direction:
            return (
                f"observed field {field!r} is {position}; "
                f"iterate direction must be {expected_direction}"
            )
        plan.direction = expected_direction
        return ""

    @staticmethod
    def _offscreen_observed_field(
        statement: StatementContract,
        observation: Observation,
    ) -> tuple[str, str] | None:
        controls = observation.form_control_state or observation.form_controls or []
        for field in statement.observe_fields:
            target = field.strip().casefold()
            for control in controls:
                if not isinstance(control, dict):
                    continue
                identities = {
                    str(control.get(key) or "").strip().casefold()
                    for key in ("label", "name", "id", "group_field")
                }
                position = str(control.get("viewport_pos") or "").strip().casefold()
                rect = control.get("rect") or {}
                if not position and isinstance(rect.get("y"), (int, float)):
                    if rect["y"] < 0:
                        position = "above"
                if target in identities and position in {"above", "below"}:
                    return field, position
        return None

    def _offscreen_observed_field_step(
        self,
        statement: StatementContract,
        observation: Observation,
        *,
        execution_scope: str,
    ) -> SupervisorStep | None:
        target = self._offscreen_observed_field(statement, observation)
        if target is None:
            return None
        field, position = target
        direction = "up" if position == "above" else "down"
        direction_text = "向上" if direction == "up" else "向下"
        summary = f"{field} 字段位于当前视口{direction_text}方，需定向滚动"
        instruction = f"在当前页面{direction_text}滚动，将 {field} 字段带入可观察视口。"
        return self._mechanical_step(
            statement,
            execution_scope=execution_scope,
            summary=summary,
            established=f"结构化控件状态确认 {field} 位于视口 {position}。",
            gap=f"{field} 尚未进入可观察视口。",
            reason="offscreen observed fields use deterministic viewport transport",
            instruction=instruction,
            role="iterate",
            family="iterate",
            target_control=field,
            expected_result=f"{field} 字段进入当前视口并可读取。",
            direction=direction,
        )

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
        target_ref: str = "",
        direction: Literal["up", "down"] | None = None,
    ) -> SupervisorStep:
        action = {
            "instruction": instruction,
            "atomic_role": role,
            "action_family": family,
            "target_control": target_control,
            "target_value": "",
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
                target_ref=target_ref,
                expected_result=expected_result,
                direction=direction,
            ),
            summary=summary,
            execution_scope=execution_scope,
            **_ctx(statement),
        )

    @staticmethod
    def _validate_input_matches(
        statement: StatementContract,
        observation: Observation,
    ) -> str:
        controls = [
            control
            for control in observation.form_controls or []
            if isinstance(control, dict)
        ]
        for field, expected in statement.inputs.items():
            if not isinstance(expected, (str, int, float, bool)):
                continue
            target = field.strip().casefold()
            for control in controls:
                identities = {
                    str(control.get(key) or "").strip().casefold()
                    for key in ("label", "name", "id", "group_field")
                }
                if target not in identities:
                    continue
                key = "selected_text" if "selected_text" in control else "value"
                if key not in control:
                    continue
                actual = str(control[key]).strip()
                if actual.casefold() != str(expected).strip().casefold():
                    return (
                        f"current {field!r} value {actual!r} does not exactly match "
                        f"input {expected!r}"
                    )
        return ""

    @staticmethod
    def _validate_filter_scope(
        statement: StatementContract,
        observation: Observation,
    ) -> str:
        declared: list[tuple[set[str], JsonValue]] = []
        for name, value in statement.required_values.items():
            terms = _semantic_terms(name)
            if "filter" not in terms:
                continue
            field_terms = terms - {"filter", "from", "to", "min", "max"}
            if field_terms:
                declared.append((field_terms, value))
        if not declared or not observation.applied_filters:
            return ""

        actual = {
            name: (_semantic_terms(name), str(value).strip().casefold())
            for name, value in observation.applied_filters.items()
        }
        matched: set[str] = set()
        missing: list[str] = []
        mismatched: list[str] = []
        for expected_terms, expected in declared:
            candidate = next(
                (
                    (name, value)
                    for name, (terms, value) in actual.items()
                    if expected_terms <= terms or terms <= expected_terms
                ),
                None,
            )
            label = " ".join(sorted(expected_terms))
            if candidate is None:
                missing.append(label)
                continue
            name, value = candidate
            matched.add(name)
            expected_value = str(expected).strip().casefold()
            endpoints = [part.strip() for part in value.split("-")]
            if value != expected_value and not (
                len(endpoints) == 2
                and all(endpoint == expected_value for endpoint in endpoints)
            ):
                mismatched.append(f"{name}={value!r}")

        unexpected = [name for name in actual if name not in matched]
        problems = []
        if missing:
            problems.append("missing " + ", ".join(missing))
        if mismatched:
            problems.append("mismatched " + ", ".join(mismatched))
        if unexpected:
            problems.append("unexpected " + ", ".join(unexpected))
        if not problems:
            return ""
        return "filter scope does not exactly match the contract: " + "; ".join(
            problems
        )

    @staticmethod
    def _staged_input_submission(
        observation: Observation,
        history: list[PolicyTurn],
        view: StatementObservationView,
    ) -> tuple[PolicyTurn, dict] | None:
        prior = next((turn for turn in reversed(history) if turn.executed), None)
        if prior is None or prior.supervisor is None:
            return None
        intent = prior.supervisor.action_intent
        if intent is None or intent.family != "input" or not intent.target_value:
            return None
        target_label = intent.target_control.strip().casefold()
        if not any(marker in target_label for marker in _QUERY_MARKERS):
            return None

        controls = [
            *(observation.form_control_state or []),
            *(observation.form_controls or []),
        ]
        matched_control = next((
            control for control in controls
            if isinstance(control, dict)
            and target_label in {
                str(control.get(key) or "").strip().casefold()
                for key in ("label", "name", "id", "group_field")
            }
        ), None)
        if matched_control is None or not (
            matched_control.get("is_filter") is True
            or str(matched_control.get("kind") or "").strip().casefold()
            in {"search", "search_input", "searchbox"}
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

        candidates: list[dict] = []
        for affordance in view.affordances:
            if "activate" not in (affordance.get("supported_operations") or []):
                continue
            label = str(affordance.get("label") or "").strip()
            if not label:
                continue
            lowered = label.casefold()
            if any(marker in lowered for marker in _MUTATING_MARKERS):
                continue
            if any(marker in lowered for marker in _QUERY_SUBMIT_MARKERS):
                candidates.append(affordance)
        if len(candidates) != 1:
            return None
        return prior, candidates[0]

    def _staged_input_submission_step(
        self,
        statement: StatementContract,
        observation: Observation,
        history: list[PolicyTurn],
        view: StatementObservationView,
        *,
        execution_scope: str,
    ) -> SupervisorStep | None:
        staged = self._staged_input_submission(observation, history, view)
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
        candidates = _action_targets(view, plan)
        if plan.target_ref:
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

    @staticmethod
    def _validate_lookup_action(
        statement: StatementContract,
        view: StatementObservationView,
        plan: _ActionDraft,
    ) -> str:
        if not isinstance(statement.inputs.get("lookup_request"), dict):
            return ""
        if plan.atomic_role == "commit":
            return "query-only lookup cannot commit business state"
        if plan.action_family == "navigate":
            return "query-only lookup cannot leave the current business context"
        if plan.action_family == "iterate":
            return ""

        candidates = _action_targets(view, plan)
        if len(candidates) != 1:
            return "query-only lookup requires one structurally identified local control"
        target = candidates[0]
        label = str(target.get("label") or "").strip().casefold()
        if any(marker in label for marker in _MUTATING_MARKERS):
            return "query-only lookup cannot activate a business mutation control"
        if plan.action_family in {"input", "select"}:
            if target.get("is_filter") is True or target.get("role") in {
                "search", "search_input", "searchbox",
            }:
                return ""
            return "query-only lookup may write only to a structural search/filter control"
        if plan.action_family == "activate" and any(
            marker in label for marker in _QUERY_SUBMIT_MARKERS
        ):
            return ""
        return "query-only lookup allows only local search/filter submission or viewport movement"

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
        rejection = self._validate_observed_field_write(statement, plan)
        if rejection:
            return None, rejection
        rejection = self._validate_observed_field_visibility(
            statement, observation, plan
        )
        if rejection:
            return None, rejection
        view = build_observation_view(statement, observation, [])
        rejection = self._validate_lookup_action(statement, view, plan)
        if rejection:
            return None, rejection
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
        lookup_scope = _resolved_lookup(statement, observation)
        if lookup_scope is not None:
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
        transport = self._offscreen_observed_field_step(
            statement,
            observation,
            execution_scope=execution_scope,
        )
        if transport is not None:
            return transport
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
        submission = self._staged_input_submission_step(
            statement,
            observation,
            scoped_history,
            view,
            execution_scope=execution_scope,
        )
        if submission is not None:
            return submission
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
                        "若当前仍有可执行路径，必须返回 act 而不是 infeasible。"
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
            rejection = self._validate_filter_scope(statement, observation)
            if not rejection:
                rejection = self._validate_input_matches(statement, observation)
            lookup_scope = _resolved_lookup(statement, observation)
            if (
                not rejection
                and isinstance(statement.inputs.get("lookup_request"), dict)
                and lookup_scope is None
            ):
                rejection = (
                    "current observation does not resolve exactly one structural "
                    "collection for the lookup request"
                )
            if rejection:
                if validation_retries > 0:
                    self._static_constraints.append(
                        "上一个 complete 候选未通过确定性终态校验："
                        f"{rejection}。继续执行动作，使当前 UI 状态精确满足合同。"
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
                return self._transition_failure(
                    statement,
                    decision,
                    rejection,
                    execution_scope=execution_scope,
                )
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
                    outputs={"scope": lookup_scope} if lookup_scope is not None else {},
                    observation=observation,
                    observation_url=observation.url,
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
