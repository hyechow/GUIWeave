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


def test_atomic_instruction_does_not_treat_value_31_as_numbered_sequence() -> None:
    assert not StatementSupervisorPolicy._is_sequence(
        "Click Save Attribute to persist options 30 and 31."
    )
    assert StatementSupervisorPolicy._is_sequence(
        "Input 'size' into Attribute Code and click Search."
    )
    assert StatementSupervisorPolicy._is_sequence(
        "在 Attribute Code 输入 size，然后点击 Search"
    )
    assert StatementSupervisorPolicy._is_sequence(
        "1. Click Add\n2. Fill the new row"
    )


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
        effect_mode="transform",
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

    assert step.should_act is True
    assert step.outcome is None
    assert step.target_control == "Status"
    assert step.atomic_role == "write"
    assert len(policy._last_transition_record["guard_rejections"]) == 1
    assert retry_inputs[0] == ""
    assert "否决了上一提议" in retry_inputs[1]


def test_guard_exhaustion_is_terminal_not_a_running_noop(monkeypatch) -> None:
    statement = StatementContract(
        id="s1",
        name="persist status",
        description="",
        kind="action",
        success_condition="Status is saved",
        effect_mode="transform",
        persistence="explicit_commit",
        target_values={"Status": "Active"},
    )
    policy = _policy(monkeypatch, statement)
    monkeypatch.setattr(policy, "_invoke_statement_transition", lambda *a, **k: _complete())

    step = policy._run_single_turn(statement, _observation(), [])

    assert step.should_act is False
    assert step.outcome is not None
    assert step.outcome.phase == "exhausted"
    assert "未能产生合法动作或终态" in step.outcome.summary


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
    assert step.should_act is False


def test_runtime_validates_contract_scope_without_choosing_write_order(monkeypatch) -> None:
    statement = StatementContract(
        id="s1",
        name="ensure option 30",
        description="",
        kind="action",
        success_condition="both fields are 30",
        effect_mode="ensure",
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

    assert step.should_act is True
    assert step.target_control == "Admin Swatch"
    assert step.target_value == "30"


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

    assert step.should_act is True
    assert step.target_control == "Attribute Code"
    assert step.target_value == "size"


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

    assert step.should_act is False
    assert step.outcome is not None
    assert step.outcome.phase == "exhausted"
    assert "allowed=['Complete', 'Pending']" in step.outcome.summary
