from __future__ import annotations

from gui_agent.core.schemas import ActionIntent

from gui_agent.core.run.flow import evaluate_turn_progress
from gui_agent.core.schemas import SupervisorStep


def _step(*, should_act: bool = True, statement_id: str | None = "m1") -> SupervisorStep:
    return SupervisorStep(action_intent=ActionIntent(instruction='test action') if should_act else None, summary='s', statement_id=statement_id)


def test_action_execution_failure_returns_to_statement():
    decision = evaluate_turn_progress(
        sv_step=_step(),
        executed=False,
    )

    assert decision.stop_reason is None


def test_successful_action_continues():
    decision = evaluate_turn_progress(
        sv_step=_step(should_act=True, statement_id="m2"),
        executed=True,
    )

    assert decision.stop_reason is None


def test_no_action_running_turn_stops_immediately():
    decision = evaluate_turn_progress(
        sv_step=_step(should_act=False, statement_id="m1"),
        executed=False,
    )

    assert decision.stop_reason == "运行中的 Statement 未产生动作或终态"


def test_invalid_transition_output_retries_without_stopping():
    decision = evaluate_turn_progress(
        sv_step=SupervisorStep(
            summary="Transition output invalid",
            statement_id="m1",
            retry_transition=True,
        ),
        executed=False,
    )

    assert decision.stop_reason is None
