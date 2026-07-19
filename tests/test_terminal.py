from gui_agent.core.orchestrator import Data, Finish, Interact, OutputSpec, Program, ValueRef
from gui_agent.core.run.flow import finish_terminal_step
from gui_agent.core.run.program_runtime import ProgramRuntime
from gui_agent.core.run.result import make_result, orchestration_result
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


def test_program_verification_uses_finish_output_lineage_not_unrelated_preflight(monkeypatch):
    runtime = ProgramRuntime.start(Program(
        goal="return an answer",
        statements=[
            Data(
                id="inspect",
                bind="probe",
                goal="inspect source",
                mode="inspect",
                returns={"available": OutputSpec(type="boolean")},
            ),
            Data(
                id="answer",
                bind="answer",
                goal="derive answer",
                returns={"value": OutputSpec(type="text")},
            ),
            Finish(outputs={"result": ValueRef(var="answer", path=["value"])}),
        ],
    ))
    monkeypatch.setattr(
        "gui_agent.core.llm.output.compose_orchestration_reply",
        lambda _goal, _digest, *, current, terminal: terminal,
    )

    runtime.send_outcome(StatementOutcome.completed(
        "preflight",
        verification="accepted_unverified",
        outputs={"available": True},
    ))
    runtime.send_outcome(StatementOutcome.completed(
        "answer",
        verification="confirmed",
        outputs={"value": "ready"},
    ))

    result = orchestration_result(
        _context(), runtime.interpreter, runtime.reply or "", current=runtime.current,
    )

    assert result.phase == "completed"
    assert result.verification == "confirmed"


def test_program_verification_propagates_unverified_finish_input(monkeypatch):
    runtime = ProgramRuntime.start(Program(
        goal="return a derived answer",
        statements=[
            Data(
                id="read",
                bind="source",
                goal="read source",
                returns={"value": OutputSpec(type="text")},
            ),
            Data(
                id="derive",
                bind="answer",
                goal="derive answer",
                inputs={"source": ValueRef(var="source", path=["value"])},
                returns={"value": OutputSpec(type="text")},
            ),
            Finish(outputs={"result": ValueRef(var="answer", path=["value"])}),
        ],
    ))
    monkeypatch.setattr(
        "gui_agent.core.llm.output.compose_orchestration_reply",
        lambda _goal, _digest, *, current, terminal: terminal,
    )

    runtime.send_outcome(StatementOutcome.completed(
        "visual read",
        verification="accepted_unverified",
        outputs={"value": "raw"},
    ))
    runtime.send_outcome(StatementOutcome.completed(
        "derived",
        verification="confirmed",
        outputs={"value": "ready"},
    ))

    result = orchestration_result(
        _context(), runtime.interpreter, runtime.reply or "", current=runtime.current,
    )

    assert runtime.interpreter.run_log[-1].result.verification == "accepted_unverified"
    assert result.phase == "completed"
    assert result.verification == "accepted_unverified"
