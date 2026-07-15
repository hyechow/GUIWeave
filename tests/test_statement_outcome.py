"""Terminal StatementOutcome invariants and supervisor-step mapping."""

from __future__ import annotations

import pytest

from gui_agent.core.run.statements.outcome import (
    StatementOutcome,
    statement_outcome_from_supervisor_step,
)
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


def test_map_supervisor_completed_step():
    step = SupervisorStep(
        should_act=False,
        instruction=None,
        stop=False,
        goal_completed=True,
        summary="保存成功",
        completion_status="confirmed",
    )
    out = statement_outcome_from_supervisor_step(step)
    assert out is not None
    assert out.phase == "completed"
    assert out.verification == "confirmed"


def test_map_supervisor_accepted_unverified():
    step = SupervisorStep(
        should_act=False,
        instruction=None,
        stop=False,
        goal_completed=True,
        summary="已提交",
        completion_status="accepted_unverified",
    )
    out = statement_outcome_from_supervisor_step(step)
    assert out is not None
    assert out.verification == "accepted_unverified"
    assert out.to_run_result().completion_status == "accepted_unverified"


def test_map_supervisor_infeasible_kickback():
    step = SupervisorStep(
        should_act=False,
        instruction=None,
        stop=True,
        goal_completed=False,
        summary="控件不可达",
        replan_directive="改走订单列表检索",
    )
    out = statement_outcome_from_supervisor_step(step)
    assert out is not None
    assert out.phase == "infeasible"
    assert "订单列表" in (out.kickback or "")


def test_map_supervisor_mid_loop_has_no_terminal_outcome():
    step = SupervisorStep(
        should_act=True,
        instruction="点击保存",
        stop=False,
        goal_completed=False,
        summary="准备点击",
    )
    assert statement_outcome_from_supervisor_step(step) is None


def test_map_stop_without_complete_to_failed_or_exhausted():
    failed = statement_outcome_from_supervisor_step(
        SupervisorStep(
            should_act=False,
            instruction=None,
            stop=True,
            goal_completed=False,
            stop_reason="找不到目标控件",
            summary="放弃",
        )
    )
    assert failed is not None and failed.phase == "failed"

    exhausted = statement_outcome_from_supervisor_step(
        SupervisorStep(
            should_act=False,
            instruction=None,
            stop=True,
            goal_completed=False,
            stop_reason="达到最大轮数 20",
            summary="超时",
        )
    )
    assert exhausted is not None and exhausted.phase == "exhausted"
