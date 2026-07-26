"""Terminal StatementOutcome invariants and native supervisor ownership."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from gui_agent.core.run.statements.outcome import StatementOutcome
from gui_agent.core.schemas import ActionIntent, PolicyTurn, SupervisorStep


def test_completed_requires_verification():
    ok = StatementOutcome.completed("done", verification="confirmed")
    assert ok.phase == "completed"
    assert ok.verification == "confirmed"
    assert ok.is_completed


def test_failed_rejects_verification():
    with pytest.raises(ValueError, match="verification"):
        StatementOutcome(phase="failed", summary="x", verification="confirmed")


def test_failed_is_a_terminal_outcome():
    out = StatementOutcome.failed("blocked")
    assert out.phase == "failed"
    assert out.failure_evidence == "blocked"
    assert not out.is_completed


def test_no_running_phase_constructor():
    with pytest.raises(ValueError):
        StatementOutcome(phase="running", summary="mid")  # type: ignore[arg-type]


def test_retired_boolean_terminal_shape_is_rejected():
    with pytest.raises(ValueError):
        StatementOutcome(
            phase="completed",
            summary="done",
            verification="confirmed",
            completed=True,  # type: ignore[call-arg]
            failed=False,  # type: ignore[call-arg]
        )


def test_supervisor_step_carries_native_outcome():
    outcome = StatementOutcome.completed(
        "已提交",
        verification="accepted_unverified",
    )
    step = SupervisorStep(summary='已提交', outcome=outcome)
    assert step.outcome is outcome
    assert step.outcome.verification == "accepted_unverified"


def test_mid_loop_step_has_no_outcome():
    step = SupervisorStep(action_intent=ActionIntent(instruction='点击保存'), summary='准备点击')
    assert step.outcome is None


def test_action_intent_is_the_only_persisted_action_shape():
    step = SupervisorStep(
        action_intent=ActionIntent(
            instruction="点击 Search",
            role="commit",
            family="activate",
            target_control="Search",
        ),
        summary="提交筛选",
    )

    payload = step.model_dump(mode="json")
    assert payload["action_intent"]["target_control"] == "Search"
    for retired in (
        "should_act",
        "instruction",
        "atomic_role",
        "action_family",
        "target_control",
        "target_value",
        "direction",
        "drag_column",
        "drag_steps",
    ):
        assert retired not in SupervisorStep.model_fields
        assert retired not in payload
    assert "action_plan" not in PolicyTurn.model_fields


def test_action_intent_is_frozen():
    intent = ActionIntent(instruction="点击 Search", target_control="Search")
    with pytest.raises(ValidationError, match="frozen"):
        intent.target_control = "Add"  # type: ignore[misc]


@pytest.mark.parametrize("running_field", ["action_intent", "is_loading"])
def test_terminal_step_rejects_running_signals(running_field):
    fields = {
        "summary": "done",
        "outcome": StatementOutcome.completed("done"),
        running_field: (
            ActionIntent(instruction="continue")
            if running_field == "action_intent"
            else True
        ),
    }
    with pytest.raises(ValueError, match="terminal SupervisorStep"):
        SupervisorStep(**fields)


def test_retired_terminal_fields_are_rejected():
    with pytest.raises(ValueError):
        SupervisorStep(summary='done', goal_completed=True)
    with pytest.raises(ValueError):
        SupervisorStep(summary='blocked', replan_directive='retry')
