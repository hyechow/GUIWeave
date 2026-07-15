from __future__ import annotations

from gui_agent.core.orchestrator.program import Program, Run
from gui_agent.core.run.flow import finish_terminal_step
from gui_agent.core.run.program_runtime import ProgramRuntime
from gui_agent.core.run.result import make_result
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
        supervisor_policy_name="statement",
        action_policy_name="browser",
        journal={"events": [
            PolicyTurn(
                index=1,
                observation_source="screen.png",
                supervisor=step,
                executed=False,
            )
        ]},
    )


def _runtime() -> ProgramRuntime:
    program = Program(goal="完成任务", statements=[Run(name="执行任务", kind="action")])
    return ProgramRuntime.start(program)


def test_finish_terminal_step_flushes_and_returns_goal_result(monkeypatch):
    step = SupervisorStep(
        should_act=False,
        outcome=StatementOutcome.completed("任务完成"),
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
        end_statement=lambda _outcome: None,
    )

    assert read_state.calls == ["drain", ("flush", 3)]
    assert messages == ["\n目标已达成：任务完成"]
    assert result["wrapped"]["phase"] == "completed"
    assert result["wrapped"]["verification"] == "confirmed"
    assert result["wrapped"]["collection_context"] is None


def test_finish_terminal_step_flushes_and_returns_stop_result(monkeypatch):
    step = SupervisorStep(
        should_act=False,
        outcome=StatementOutcome.failed("用户停止"),
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
        end_statement=lambda _outcome: None,
    )

    assert read_state.calls == ["drain", ("flush", 4)]
    assert messages == ["\n任务未完成：用户停止"]
    assert result["phase"] == "failed"
    assert result["verification"] is None
    assert "用户停止" in result["stop_reason"]


def test_base_result_does_not_promote_one_completed_statement_to_program_success():
    step = SupervisorStep(
        should_act=False,
        outcome=StatementOutcome.completed("第一步完成"),
        summary="第一步完成",
    )

    result = make_result(_context(step), "用户中途退出")

    assert result["phase"] == "stopped"
    assert result["verification"] is None
