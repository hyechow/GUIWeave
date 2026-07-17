from gui_agent.core.orchestrator import Interact, Program
from gui_agent.core.run.flow import finish_terminal_step
from gui_agent.core.run.program_runtime import ProgramRuntime
from gui_agent.core.run.result import make_result
from gui_agent.core.schemas import PolicyContext, StatementOutcome


def _context() -> PolicyContext:
    return PolicyContext(
        goal="complete task",
        supervisor_policy_name="statement",
        action_policy_name="test",
    )


def _runtime() -> ProgramRuntime:
    return ProgramRuntime.start(
        Program(
            goal="complete task",
            statements=[
                Interact(id="s1", goal="perform task", success="task is complete")
            ],
        )
    )


def test_terminal_completion_advances_program_without_reader_lifecycle(monkeypatch):
    messages = []
    ended = []
    runtime = _runtime()
    monkeypatch.setattr(
        "gui_agent.core.llm.output.compose_orchestration_reply",
        lambda _goal, _digest, *, current, terminal: terminal,
    )

    result = finish_terminal_step(
        outcome=StatementOutcome.completed("task complete"),
        read_state=None,
        turn_no=3,
        program_runtime=runtime,
        context=_context(),
        finish=lambda value: value,
        say=messages.append,
        end_statement=ended.append,
    )

    assert messages == ["\n目标已达成：task complete"]
    assert ended[0].phase == "completed"
    assert result.phase == "completed"
    assert result.verification == "confirmed"


def test_terminal_failure_stays_program_failure(monkeypatch):
    runtime = _runtime()
    monkeypatch.setattr(
        "gui_agent.core.llm.output.compose_orchestration_reply",
        lambda _goal, _digest, *, current, terminal: terminal,
    )
    result = finish_terminal_step(
        outcome=StatementOutcome.failed("cannot complete"),
        read_state=None,
        turn_no=1,
        program_runtime=runtime,
        context=_context(),
        finish=lambda value: value,
        say=lambda _message: None,
        end_statement=lambda _outcome: None,
    )
    assert result.phase == "failed"
    assert result.verification is None


def test_one_statement_outcome_never_promotes_unfinished_program_result():
    result = make_result(_context(), "user stopped before Program finished")
    assert result.phase == "stopped"
    assert result.verification is None
