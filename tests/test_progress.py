from __future__ import annotations

from types import SimpleNamespace

from gui_agent.core.run.flow import evaluate_turn_progress
from gui_agent.core.schemas import BaseAction, BaseActionDecision, SupervisorStep


def _step(*, should_act: bool = True, milestone_id: str | None = "m1") -> SupervisorStep:
    return SupervisorStep(
        should_act=should_act,
        summary="s",
        milestone_id=milestone_id,
    )


def _decision(*, not_found: bool = False):
    action = BaseAction(action_type="tap", x=1, y=2, description="点")
    if not_found:
        return BaseActionDecision(action=action, not_found_reason="找不到")
    return BaseActionDecision(action=action)


def test_probe_failure_continues_until_third_failure():
    first = evaluate_turn_progress(
        noop_count=0,
        prev_milestone_id="m1",
        sv_step=_step(),
        executed=False,
        action_decision=_decision(),
        probe_failed=True,
    )
    third = evaluate_turn_progress(
        noop_count=2,
        prev_milestone_id="m1",
        sv_step=_step(),
        executed=False,
        action_decision=_decision(),
        probe_failed=True,
    )

    assert first.noop_count == 1
    assert first.continue_loop is True
    assert first.message == "滚动探测失败，进入下一轮重新规划"
    assert third.stop_reason == "连续 3 轮滚动探测失败"
    assert third.stop_message == "\n连续 3 轮滚动探测失败，agent-loop 停止"


def test_not_found_counts_as_no_action():
    decision = evaluate_turn_progress(
        noop_count=2,
        prev_milestone_id="m1",
        sv_step=_step(),
        executed=False,
        action_decision=_decision(not_found=True),
        probe_failed=False,
    )

    assert decision.stop_reason == "连续 3 轮无动作"


def test_action_execution_failure_replans_until_third_failure():
    decision = evaluate_turn_progress(
        noop_count=0,
        prev_milestone_id="m1",
        sv_step=_step(),
        executed=False,
        action_decision=_decision(),
        probe_failed=False,
    )

    assert decision.continue_loop is True
    assert decision.message == "动作执行失败，进入下一轮重新规划"


def test_protocol_suppression_is_not_reported_as_executor_failure():
    first = evaluate_turn_progress(
        noop_count=0,
        prev_milestone_id="m1",
        sv_step=_step(),
        executed=False,
        action_decision=_decision(),
        probe_failed=False,
        suppressed_reason="commit already dispatched",
    )
    third = evaluate_turn_progress(
        noop_count=2,
        prev_milestone_id="m1",
        sv_step=_step(),
        executed=False,
        action_decision=_decision(),
        probe_failed=False,
        suppressed_reason="commit already dispatched",
    )

    assert first.continue_loop is True
    assert first.message == "动作被执行协议抑制，重新观察并调整计划"
    assert third.stop_reason == "连续 3 轮动作被执行协议抑制"


def test_milestone_change_resets_noop_count():
    decision = evaluate_turn_progress(
        noop_count=2,
        prev_milestone_id="m1",
        sv_step=_step(should_act=True, milestone_id="m2"),
        executed=True,
        action_decision=_decision(),
        probe_failed=False,
    )

    assert decision.noop_count == 0
    assert decision.prev_milestone_id == "m2"
    assert decision.stop_reason is None
    assert decision.continue_loop is False


def test_no_action_turn_stops_after_three_noops():
    decision = evaluate_turn_progress(
        noop_count=2,
        prev_milestone_id="m1",
        sv_step=_step(should_act=False, milestone_id="m1"),
        executed=False,
        action_decision=None,
        probe_failed=False,
    )

    assert decision.stop_reason == "连续 3 轮无动作"
    assert decision.prev_milestone_id == "m1"


def test_missing_action_stops_immediately():
    decision = evaluate_turn_progress(
        noop_count=0,
        prev_milestone_id="m1",
        sv_step=_step(),
        executed=False,
        action_decision=SimpleNamespace(action=None, not_found_reason=None),
        probe_failed=False,
    )

    assert decision.stop_reason == "动作未执行，agent-loop 停止"
    assert decision.continue_loop is False
