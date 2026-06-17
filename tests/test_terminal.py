from __future__ import annotations

from gui_agent.core.run.flow import finish_terminal_step
from gui_agent.core.schemas import PolicyContext, PolicyTurn, SupervisorStep


class _ReadState:
    def __init__(self) -> None:
        self.calls = []

    def drain_pending(self, *, say):
        self.calls.append("drain")

    def flush(self, *, turn_no, say):
        self.calls.append(("flush", turn_no))


def _context(step: SupervisorStep) -> PolicyContext:
    return PolicyContext(
        goal="完成任务",
        supervisor_policy_name="milestone",
        action_policy_name="browser",
        turns=[
            PolicyTurn(
                index=1,
                observation_source="screen.png",
                supervisor=step,
                executed=False,
            )
        ],
    )


def test_finish_terminal_step_flushes_and_returns_goal_result():
    step = SupervisorStep(
        should_act=False,
        stop=False,
        goal_completed=True,
        summary="已经完成",
        collection_summary="采集完成",
    )
    read_state = _ReadState()
    messages = []

    result = finish_terminal_step(
        sv_step=step,
        read_state=read_state,
        turn_no=3,
        program=None,
        current_run=None,
        interpreter_steps=None,
        interpreter=None,
        context=_context(step),
        notes_mark=0,
        finish=lambda value: {"wrapped": value},
        say=messages.append,
    )

    assert read_state.calls == ["drain", ("flush", 3)]
    assert messages == ["\n目标已达成：目标已达成"]
    assert result["wrapped"]["goal_completed"] is True
    assert result["wrapped"]["collection_context"] == "采集完成"


def test_finish_terminal_step_flushes_and_returns_stop_result():
    step = SupervisorStep(
        should_act=False,
        stop=True,
        stop_reason="连续无动作",
        goal_completed=False,
        summary="未完成",
    )
    read_state = _ReadState()
    messages = []

    result = finish_terminal_step(
        sv_step=step,
        read_state=read_state,
        turn_no=4,
        program=None,
        current_run=None,
        interpreter_steps=None,
        interpreter=None,
        context=_context(step),
        notes_mark=0,
        finish=lambda value: value,
        say=messages.append,
    )

    assert read_state.calls == ["drain", ("flush", 4)]
    assert messages == ["\n任务未完成：连续无动作"]
    assert result["goal_completed"] is False
    assert result["stop_reason"] == "连续无动作"
