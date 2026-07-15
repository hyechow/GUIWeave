"""Terminal StatementOutcome invariants and supervisor-step mapping."""

from __future__ import annotations

import pytest

from gui_agent.core.run.statements.outcome import (
    ExecutorDecision,
    StatementOutcome,
    executor_decision_from_supervisor_step,
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


def test_executor_decision_is_not_an_outcome():
    d = ExecutorDecision.act("点击保存", summary="plan")
    assert d.kind == "act"
    assert not hasattr(d, "phase")
    assert ExecutorDecision.observe(summary="wait frame").kind == "observe"
    assert ExecutorDecision.wait(summary="loading").kind == "wait"


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
    assert executor_decision_from_supervisor_step(step) is None


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


def test_map_supervisor_mid_loop_returns_none_outcome_and_decision():
    step = SupervisorStep(
        should_act=True,
        instruction="点击保存",
        stop=False,
        goal_completed=False,
        summary="准备点击",
    )
    assert statement_outcome_from_supervisor_step(step) is None
    decision = executor_decision_from_supervisor_step(step)
    assert decision is not None
    assert decision.kind == "act"
    assert decision.instruction == "点击保存"


def test_map_loading_to_wait_decision():
    step = SupervisorStep(
        should_act=False,
        instruction=None,
        stop=False,
        goal_completed=False,
        is_loading=True,
        summary="页面加载中",
    )
    assert statement_outcome_from_supervisor_step(step) is None
    assert executor_decision_from_supervisor_step(step).kind == "wait"


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


def test_make_run_result_bridges_via_statement_outcome():
    from gui_agent.core.orchestrator.program import Read
    from gui_agent.core.orchestrator.runner import make_run_result

    ok = make_run_result(
        Read(name="读"),
        completed=True,
        summary="ok",
        notes=[],
        reads={"a": "1"},
        completion_status="accepted_unverified",
    )
    assert ok.completed and not ok.failed
    assert ok.completion_status == "accepted_unverified"
    assert ok.reads == {"a": "1"}

    bad = make_run_result(Read(name="读"), completed=False, summary="no", notes=[])
    assert bad.failed and not bad.completed
    assert bad.completion_status == "failed"
