from __future__ import annotations

from gui_agent.core.schemas import ActionIntent

from types import SimpleNamespace

from gui_agent.core.run.flow import evaluate_turn_progress
from gui_agent.core.schemas import (
    BaseAction,
    BaseActionDecision,
    SupervisorStep,
    TargetBinding,
)


def _step(*, should_act: bool = True, statement_id: str | None = "m1") -> SupervisorStep:
    return SupervisorStep(action_intent=ActionIntent(instruction='test action') if should_act else None, summary='s', statement_id=statement_id)


def _decision(*, not_found: bool = False):
    action = BaseAction(action_type="tap", x=1, y=2, description="点")
    if not_found:
        return BaseActionDecision(action=action, not_found_reason="找不到")
    return BaseActionDecision(action=action)


def test_not_found_stops_immediately():
    decision = evaluate_turn_progress(
        sv_step=_step(),
        executed=False,
        action_decision=_decision(not_found=True),
    )

    assert decision.stop_reason == "动作目标未找到：找不到"


def test_action_execution_failure_stops_immediately():
    decision = evaluate_turn_progress(
        sv_step=_step(),
        executed=False,
        action_decision=_decision(),
    )

    assert decision.stop_reason == "动作执行失败"


def test_protocol_suppression_is_not_reported_as_executor_failure():
    decision = evaluate_turn_progress(
        sv_step=_step(),
        executed=False,
        action_decision=_decision(),
        suppressed_reason="commit already dispatched",
    )
    assert decision.stop_reason == "动作被执行协议抑制：commit already dispatched"


def test_structural_target_contradiction_is_recoverable():
    decision = evaluate_turn_progress(
        sv_step=_step(),
        executed=False,
        action_decision=_decision(),
        suppressed_reason="point belongs to Visible",
        binding=TargetBinding(
            status="contradicted",
            source="structural",
            reason="point belongs to Visible",
        ),
    )

    assert decision.stop_reason is None


def test_successful_action_continues():
    decision = evaluate_turn_progress(
        sv_step=_step(should_act=True, statement_id="m2"),
        executed=True,
        action_decision=_decision(),
    )

    assert decision.stop_reason is None


def test_no_action_running_turn_stops_immediately():
    decision = evaluate_turn_progress(
        sv_step=_step(should_act=False, statement_id="m1"),
        executed=False,
        action_decision=None,
    )

    assert decision.stop_reason == "运行中的 Statement 未产生动作或终态"


def test_missing_action_stops_immediately():
    decision = evaluate_turn_progress(
        sv_step=_step(),
        executed=False,
        action_decision=SimpleNamespace(action=None, not_found_reason=None),
    )

    assert decision.stop_reason == "动作未执行，agent-loop 停止"
