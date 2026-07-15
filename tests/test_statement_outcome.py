"""Terminal StatementOutcome invariants and native supervisor ownership."""

from __future__ import annotations

import pytest

from gui_agent.core.run.statements.outcome import StatementOutcome
from gui_agent.core.schemas import SupervisorStep


def test_completed_requires_verification():
    ok = StatementOutcome.completed("done", verification="confirmed")
    assert ok.phase == "completed"
    assert ok.verification == "confirmed"
    assert ok.is_completed
    assert ok.to_run_result().completed is True
    assert ok.to_run_result().failed is False
    assert ok.to_run_result().completion_status == "confirmed"


def test_completed_rejects_kickback():
    with pytest.raises(ValueError, match="kickback"):
        StatementOutcome(
            phase="completed",
            summary="x",
            verification="confirmed",
            kickback="go elsewhere",
        )


def test_failed_rejects_verification_and_kickback():
    with pytest.raises(ValueError, match="verification"):
        StatementOutcome(phase="failed", summary="x", verification="confirmed")
    with pytest.raises(ValueError, match="kickback"):
        StatementOutcome(phase="failed", summary="x", kickback="nope")


def test_infeasible_requires_kickback():
    with pytest.raises(ValueError, match="kickback"):
        StatementOutcome.infeasible("blocked", kickback="")
    out = StatementOutcome.infeasible("blocked", kickback="use list view")
    assert out.phase == "infeasible"
    assert out.kickback == "use list view"
    assert out.to_run_result().failed is True
    assert out.to_run_result().completed is False


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
    step = SupervisorStep(
        should_act=False,
        summary="已提交",
        outcome=outcome,
    )
    assert step.outcome is outcome
    assert step.outcome.verification == "accepted_unverified"


def test_mid_loop_step_has_no_outcome():
    step = SupervisorStep(
        should_act=True,
        instruction="点击保存",
        summary="准备点击",
    )
    assert step.outcome is None


@pytest.mark.parametrize("running_field", ["should_act", "is_loading"])
def test_terminal_step_rejects_running_signals(running_field):
    fields = {
        "should_act": False,
        "summary": "done",
        "outcome": StatementOutcome.completed("done"),
        running_field: True,
    }
    with pytest.raises(ValueError, match="terminal SupervisorStep"):
        SupervisorStep(**fields)


def test_retired_terminal_fields_are_rejected():
    with pytest.raises(ValueError):
        SupervisorStep(
            should_act=False,
            summary="done",
            goal_completed=True,  # type: ignore[call-arg]
        )
    with pytest.raises(ValueError):
        SupervisorStep(
            should_act=False,
            summary="blocked",
            replan_directive="retry",  # type: ignore[call-arg]
        )
