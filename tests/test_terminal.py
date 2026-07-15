from __future__ import annotations

from gui_agent.core.orchestrator.program import Program, Run
from gui_agent.core.run.flow import finish_terminal_step
from gui_agent.core.run.program_runtime import ProgramRuntime
from gui_agent.core.run.statements.outcome import StatementOutcome
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


def _runtime() -> ProgramRuntime:
    program = Program(goal="完成任务", statements=[Run(name="执行任务", kind="action")])
    return ProgramRuntime.start(program)


def test_finish_terminal_step_flushes_and_returns_goal_result(monkeypatch):
    step = SupervisorStep(
        should_act=False,
        stop=False,
        goal_completed=True,
        summary="已经完成",
        collection_summary="采集完成",
    )
    read_state = _ReadState()
    messages = []
    rt = _runtime()
    monkeypatch.setattr(
        "gui_agent.core.llm.output.compose_orchestration_reply",
        lambda _goal, _digest, *, current, terminal: terminal,
    )

    result = finish_terminal_step(
        outcome=StatementOutcome.completed("任务完成"),
        read_state=read_state,
        turn_no=3,
        program_runtime=rt,
        context=_context(step),
        finish=lambda value: {"wrapped": value},
        say=messages.append,
    )

    assert read_state.calls == ["drain", ("flush", 3)]
    assert messages == ["\n目标已达成：任务完成"]
    assert result["wrapped"]["goal_completed"] is True
    assert result["wrapped"]["collection_context"] is None


def test_finish_terminal_step_flushes_and_returns_stop_result(monkeypatch):
    step = SupervisorStep(
        should_act=False,
        stop=True,
        stop_reason="连续无动作",
        goal_completed=False,
        summary="未完成",
    )
    read_state = _ReadState()
    messages = []
    rt = _runtime()
    monkeypatch.setattr(
        "gui_agent.core.llm.output.compose_orchestration_reply",
        lambda _goal, _digest, *, current, terminal: terminal,
    )

    result = finish_terminal_step(
        outcome=StatementOutcome.failed("用户停止"),
        read_state=read_state,
        turn_no=4,
        program_runtime=rt,
        context=_context(step),
        finish=lambda value: value,
        say=messages.append,
    )

    assert read_state.calls == ["drain", ("flush", 4)]
    assert messages == ["\n任务未完成：用户停止"]
    assert result["goal_completed"] is False
    assert "用户停止" in result["stop_reason"]
