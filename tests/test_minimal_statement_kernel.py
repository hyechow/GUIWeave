from __future__ import annotations

from gui_agent.core.schemas import Observation, StatementContract
from gui_agent.core.supervisor.statement.policy import StatementSupervisorPolicy
from gui_agent.core.supervisor.statement.schemas import (
    _StatementTransitionResult,
    _TransitionAction,
    _TransitionEvidence,
)


INSTANCE = "test:minimal-kernel"


def _observation() -> Observation:
    return Observation(
        png_bytes=b"frame",
        source="test",
        title="Target page",
        url="https://example.test/target",
    )


def _complete(reason: str = "the target is visible") -> _StatementTransitionResult:
    return _StatementTransitionResult(
        kind="complete",
        reason=reason,
        evidence=[
            _TransitionEvidence(source="current_observation", claim=reason)
        ],
    )


def _act(
    instruction: str = "click the target",
    *,
    family: str = "activate",
    control: str = "",
    value: str = "",
) -> _StatementTransitionResult:
    return _StatementTransitionResult(
        kind="act",
        reason="the contract remains open",
        action=_TransitionAction(
            instruction=instruction,
            action_family=family,
            target_control=control,
            target_value=value,
        ),
    )


def _policy(monkeypatch, statement: StatementContract) -> StatementSupervisorPolicy:
    policy = StatementSupervisorPolicy()
    policy.begin_statement(statement, instance_id=INSTANCE)
    monkeypatch.setattr(
        "gui_agent.core.supervisor.statement.policy.is_loading_frame",
        lambda _observation: False,
    )
    return policy


def test_transition_vocabulary_is_minimal() -> None:
    kind = _StatementTransitionResult.model_json_schema()["properties"]["kind"]
    assert kind["enum"] == ["act", "complete", "infeasible"]


def test_transition_schema_emits_action_before_reason() -> None:
    properties = list(_StatementTransitionResult.model_json_schema()["properties"])
    assert properties.index("action") < properties.index("reason")


def test_typed_input_ignores_extra_prose_actions(monkeypatch) -> None:
    statement = StatementContract(
        id="s1",
        name="filter attributes",
        description="",
        kind="filter",
        success_condition="Attribute Code=size is applied",
        target_values={"Attribute Code": "size"},
    )
    policy = _policy(monkeypatch, statement)
    monkeypatch.setattr(
        policy,
        "_invoke_statement_transition",
        lambda *a, **k: _act(
            "Input 'size' into Attribute Code and click Search.",
            family="input",
            control="Attribute Code text_input (name='attribute_code')",
            value="size",
        ),
    )

    step = policy._run_single_turn(statement, _observation(), [])

    assert step.outcome is None
    assert step.action_intent is not None
    assert (
        step.action_intent.instruction
        == "Input 'size' into the visible 'Attribute Code' control."
    )
    assert step.action_intent.target_control == "Attribute Code"


def test_navigation_completion_uses_runtime_verification(monkeypatch) -> None:
    statement = StatementContract(
        id="s1",
        name="reach target page",
        description="",
        kind="navigation",
        success_condition="Target page is visible",
    )
    policy = _policy(monkeypatch, statement)
    monkeypatch.setattr(policy, "_invoke_statement_transition", lambda *a, **k: _complete())

    step = policy._run_single_turn(statement, _observation(), [])

    assert step.outcome is not None
    assert step.outcome.phase == "completed"
    assert step.outcome.verification == "accepted_unverified"
    assert step.pre_existing is True


def test_rejected_complete_is_redecided_on_same_frame(monkeypatch) -> None:
    statement = StatementContract(
        id="s1",
        name="set status",
        description="",
        kind="action",
        success_condition="Status is Active and saved",
        persistence="explicit_commit",
        target_values={"Status": "Active"},
    )
    policy = _policy(monkeypatch, statement)
    decisions = iter([
        _complete("Status looks Active"),
        _act(
            "set Status to Active",
            family="select",
            control="Status",
            value="Active",
        ),
    ])
    retry_inputs: list[str] = []

    def decide(*_args, **kwargs):
        retry_inputs.append(kwargs.get("extra", ""))
        return next(decisions)

    monkeypatch.setattr(
        policy,
        "_invoke_statement_transition",
        decide,
    )

    step = policy._run_single_turn(statement, _observation(), [])

    assert step.action_intent is not None
    assert step.outcome is None
    assert step.action_intent.target_control == "Status"
    assert step.action_intent.role == "write"
    assert len(policy._last_transition_record["guard_rejections"]) == 1
    assert retry_inputs[0] == ""
    assert "Statement-local replan" in retry_inputs[1]


def test_guard_exhaustion_is_terminal_not_a_running_noop(monkeypatch) -> None:
    statement = StatementContract(
        id="s1",
        name="persist status",
        description="",
        kind="action",
        success_condition="Status is saved",
        persistence="explicit_commit",
        target_values={"Status": "Active"},
    )
    policy = _policy(monkeypatch, statement)
    monkeypatch.setattr(policy, "_invoke_statement_transition", lambda *a, **k: _complete())

    step = policy._run_single_turn(statement, _observation(), [])

    assert step.action_intent is None
    assert step.outcome is not None
    assert step.outcome.phase == "exhausted"
    assert "Statement-local replan 后仍未产生合法动作或终态" in step.outcome.summary


def test_hard_budget_reconcile_cannot_emit_running_noop(monkeypatch) -> None:
    statement = StatementContract(
        id="s1",
        name="continue editing",
        description="",
        kind="action",
        success_condition="saved",
    )
    policy = _policy(monkeypatch, statement)
    monkeypatch.setattr(policy, "_invoke_statement_transition", lambda *a, **k: _act())

    step = policy.reconcile(_observation(), "goal", [])

    assert step.outcome is not None
    assert step.outcome.phase == "exhausted"
    assert step.action_intent is None


def test_runtime_validates_contract_scope_without_choosing_write_order(monkeypatch) -> None:
    statement = StatementContract(
        id="s1",
        name="ensure option 30",
        description="",
        kind="action",
        success_condition="both fields are 30",
        target_values={"Admin Description": "30", "Admin Swatch": "30"},
    )
    policy = _policy(monkeypatch, statement)
    # Admin Swatch is deliberately proposed before Admin Description. The kernel checks only
    # contract scope; it does not carry a deterministic next-field phase.
    monkeypatch.setattr(
        policy,
        "_invoke_statement_transition",
        lambda *a, **k: _act(
            "set Admin Swatch to 30",
            family="input",
            control="Admin Swatch",
            value="30",
        ),
    )

    step = policy._run_single_turn(statement, _observation(), [])

    assert step.action_intent is not None
    assert step.action_intent.target_control == "Admin Swatch"
    assert step.action_intent.target_value == "30"


def test_runtime_fills_one_omitted_declared_write_value(monkeypatch) -> None:
    statement = StatementContract(
        id="s1",
        name="filter by attribute code",
        description="",
        kind="filter",
        success_condition="Attribute Code filter is applied",
        target_controls=["Attribute Code"],
        target_values={"Attribute Code": "size"},
    )
    policy = _policy(monkeypatch, statement)
    monkeypatch.setattr(
        policy,
        "_invoke_statement_transition",
        lambda *a, **k: _act(
            "Type 'size' into Attribute Code",
            family="input",
            control="Attribute Code",
        ),
    )

    step = policy._run_single_turn(statement, _observation(), [])

    assert step.action_intent is not None
    assert step.action_intent.target_control == "Attribute Code"
    assert step.action_intent.target_value == "size"


def test_runtime_does_not_guess_an_ambiguous_omitted_write_value(monkeypatch) -> None:
    statement = StatementContract(
        id="s1",
        name="choose one status",
        description="",
        kind="filter",
        success_condition="Status filter is applied",
        target_controls=["Status"],
        target_values={"Status": ["Pending", "Complete"]},
    )
    policy = _policy(monkeypatch, statement)
    monkeypatch.setattr(
        policy,
        "_invoke_statement_transition",
        lambda *a, **k: _act(
            "Select a declared status",
            family="select",
            control="Status",
        ),
    )

    step = policy._run_single_turn(statement, _observation(), [])

    assert step.action_intent is None
    assert step.outcome is not None
    assert step.outcome.phase == "exhausted"
    assert "allowed=['Complete', 'Pending']" in step.outcome.summary


def test_guard_rejection_uses_statement_local_replan_for_a_prepare_route(monkeypatch) -> None:
    statement = StatementContract(
        id="s1",
        name="filter by product name",
        description="",
        kind="filter",
        success_condition="Name filter is applied",
        target_controls=["Name"],
        target_values={"Name": "Diana Tights"},
    )
    policy = _policy(monkeypatch, statement)
    decisions = iter([
        _act(
            "Type Diana Tights into Search by keyword",
            family="input",
            control="Search by keyword",
            value="Diana Tights",
        ),
        _act(
            "Open Filters",
            family="activate",
            control="Filters",
        ),
    ])
    retry_inputs: list[str] = []

    def decide(*_args, **kwargs):
        retry_inputs.append(kwargs.get("extra", ""))
        return next(decisions)

    monkeypatch.setattr(policy, "_invoke_statement_transition", decide)

    step = policy._run_single_turn(statement, _observation(), [])

    assert step.outcome is None
    assert step.action_intent is not None
    assert step.action_intent.family == "activate"
    assert step.action_intent.target_control == "Filters"
    assert "Statement-local replan" in retry_inputs[1]
    assert "没有判定 Statement 不可达" in retry_inputs[1]
    assert "Search by keyword" in retry_inputs[1]
