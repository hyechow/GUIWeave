"""ProgramRuntime ownership: always-on program, no DAG fallback."""

from __future__ import annotations

import pytest

from gui_agent.core.orchestrator.program import Finish, ForEach, Program, Run
from gui_agent.core.run.program_runtime import ProgramRuntime
from gui_agent.core.supervisor.milestone.policy import MilestoneSupervisorPolicy
from gui_agent.core.schemas import Observation


def test_program_runtime_starts_and_finishes_finish_only_program():
    program = Program(goal="done", statements=[Finish(message="ok")])
    rt = ProgramRuntime.start(program)
    assert rt.finished
    assert rt.reply == "ok"
    assert rt.current is None


def test_program_runtime_send_advances_cursor():
    program = Program(
        goal="g",
        statements=[
            Run(name="one", kind="action", var="a"),
            Run(name="two", kind="action", var="b"),
        ],
    )
    rt = ProgramRuntime.start(program)
    assert rt.current is not None and rt.current.name == "one"
    from gui_agent.core.run.statements.outcome import StatementOutcome

    nxt = rt.send_outcome(StatementOutcome.completed("done one", verification="confirmed"))
    assert nxt is not None and nxt.name == "two"
    assert rt.index == 1


def test_program_runtime_owns_recovery_and_replacement():
    from gui_agent.core.orchestrator.recovery import MAX_KICKBACK_REPLANS
    from gui_agent.core.run.statements.outcome import StatementOutcome

    program = Program(goal="g", statements=[Run(name="one", kind="action")])
    rt = ProgramRuntime.start(program)
    assert rt.begin_kickback() == 1
    for attempt in range(2, MAX_KICKBACK_REPLANS + 1):
        assert rt.begin_kickback() == attempt
    assert rt.begin_kickback() is None

    rt.send_outcome(StatementOutcome.failed("boom"))
    assert rt.finished
    assert not rt.interpreter.run_log[-1].result.is_completed
    rt.replace_program(
        Program(goal="g2", statements=[Run(name="retry", kind="action")]),
        drop_failed_from_log=True,
    )
    assert all(record.result.is_completed for record in rt.interpreter.run_log)
    assert rt.current is not None and rt.current.name == "retry"


def test_program_runtime_stamps_body_record_before_foreach_aggregate():
    """The final foreach send appends both the body RunRecord and an aggregate.

    Invocation identity belongs on the body statement, not whichever record happens to be last.
    """
    from gui_agent.core.run.statements.outcome import StatementOutcome

    body = Run(statement_id="s1", name="per row", kind="action", var="result")
    program = Program(
        goal="g",
        statements=[ForEach(var="row", over="rows", body=[body])],
    )
    rt = ProgramRuntime.start(
        program,
        collect_fn=lambda _target, _returns, *, limit=None: [{"id": "1"}],
    )
    assert rt.current is body

    instance_id = rt.next_instance_id("s1")
    rt.send_outcome(StatementOutcome.completed("done", verification="confirmed"))

    assert len(rt.interpreter.run_log) == 2
    body_record, aggregate_record = rt.interpreter.run_log
    assert body_record.name == "per row"
    assert body_record.instance_id == instance_id
    assert aggregate_record.instance_id == ""


def test_supervisor_step_without_reseed_raises():
    policy = MilestoneSupervisorPolicy()
    obs = Observation(png_bytes=b"x", source="test")
    with pytest.raises(RuntimeError, match="begin_statement"):
        policy.step(obs, "goal", [])


def test_supervisor_reseed_then_complete_does_not_walk_next_milestone():
    """Single-statement executor: completing one statement does not open the next."""
    from gui_agent.core.run.execution_signals import CompletionEvaluation
    from gui_agent.core.schemas import StatementContract

    policy = MilestoneSupervisorPolicy()
    first = StatementContract(
        id="m1",
        name="第一步",
        description="d",
        success_condition="done",
        kind="action",
    )
    policy.begin_statement(first, instance_id="i1")

    decision = CompletionEvaluation(
        status="satisfied",
        reason="ok",
        completion_status="confirmed",
        next="complete",
    )
    step = policy._advance(
        first,
        Observation(png_bytes=b"x", source="t"),
        [],
        decision=decision,
    )
    assert step.outcome is not None and step.outcome.phase == "completed"
    assert policy._active_milestone is first
