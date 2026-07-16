"""ProgramRuntime ownership: always-on program, no DAG fallback."""

from __future__ import annotations

from gui_agent.core.schemas import ActionIntent

import pytest

from gui_agent.core.orchestrator.program import Finish, ForEach, Program, Run
from gui_agent.core.run.interactive import contract_for_run
from gui_agent.core.run.program_runtime import ProgramRuntime
from gui_agent.core.run.turns import snapshot_statement_runtime
from gui_agent.core.supervisor.statement.policy import StatementSupervisorPolicy
from gui_agent.core.schemas import (
    EventJournal,
    Observation,
    PolicyTurn,
    StatementOutcome,
    StatementOutcomeEvent,
    SupervisorStep,
)


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
    nxt = rt.send_outcome(StatementOutcome.completed("done one", verification="confirmed"))
    assert nxt is not None and nxt.name == "two"
    assert rt.index == 1


def test_program_runtime_owns_recovery_and_replacement():
    from gui_agent.core.orchestrator.recovery import MAX_KICKBACK_REPLANS

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


def test_program_runtime_replays_terminal_event_into_next_statement():
    first = Run(statement_id="s1", name="one", kind="action", var="a")
    second = Run(statement_id="s2", name="two", kind="action", var="b")
    program = Program(goal="g", statements=[first, second, Finish(message="done")])
    journal = EventJournal()
    live = ProgramRuntime.start(program, journal=journal)
    instance_id = live.next_instance_id(first.statement_id)
    journal.append_statement_outcome(
        StatementOutcomeEvent(
            after_turn=0,
            observation_source="test",
            statement_instance_id=instance_id,
            statement_id=first.statement_id,
            statement_kind="action",
            outcome=StatementOutcome.completed("one completed"),
        )
    )

    resumed = ProgramRuntime.resume(program, journal)

    assert resumed.current is not None and resumed.current.name == "two"
    assert resumed.index == 1
    assert resumed.current_instance_id == ""
    assert resumed.interpreter.run_log[0].instance_id == instance_id
    assert resumed.interpreter.run_log[0].result.phase == "completed"


def test_program_runtime_replays_consecutive_outcomes_without_turns():
    first = Run(statement_id="s1", name="one", kind="action")
    second = Run(statement_id="s2", name="two", kind="action")
    program = Program(goal="g", statements=[first, second, Finish(message="done")])
    journal = EventJournal()
    ProgramRuntime.start(program, journal=journal)
    journal.append_statement_outcome(StatementOutcomeEvent(
        after_turn=0,
        statement_instance_id="i1:s1",
        statement_id="s1",
        statement_kind="action",
        outcome=StatementOutcome.completed("one completed"),
    ))
    journal.append_statement_outcome(StatementOutcomeEvent(
        after_turn=0,
        statement_instance_id="i2:s2",
        statement_id="s2",
        statement_kind="action",
        outcome=StatementOutcome.completed("two completed"),
    ))

    resumed = ProgramRuntime.resume(program, journal)

    assert resumed.finished
    assert resumed.reply == "done"
    assert journal.turns == []
    assert [record.instance_id for record in resumed.interpreter.run_log] == [
        "i1:s1",
        "i2:s2",
    ]


def test_program_and_statement_runtime_resume_from_minimal_turn_snapshot():
    run = Run(statement_id="s1", name="one", kind="action")
    program = Program(goal="g", statements=[run, Finish(message="done")])
    journal = EventJournal()
    live = ProgramRuntime.start(program, journal=journal)
    instance_id = live.next_instance_id(run.statement_id)
    contract = contract_for_run(run, 0)
    policy = StatementSupervisorPolicy()
    policy.begin_statement(contract, instance_id=instance_id)
    scope = policy._rt.execution_scope
    policy._rt.statement_info_emitted = True
    policy._initial_filters = {"status": "open"}
    snapshot = snapshot_statement_runtime(policy)
    assert snapshot is not None
    # Reader notes are journaled before the turn that reports them. Resume must retain the
    # invocation's begin boundary, rather than treating first-turn notes as pre-existing.
    journal.append_content("first-turn note")
    journal.append_turn(
        PolicyTurn(
            index=7,
            observation_source="test",
            statement_instance_id=instance_id,
            runtime_state=snapshot,
            supervisor=SupervisorStep(action_intent=ActionIntent(instruction='tap Save'), summary='still running', statement_id=run.statement_id, execution_scope=scope),
        )
    )

    resumed = ProgramRuntime.resume(program, journal)
    restored_policy = StatementSupervisorPolicy()
    assert resumed.current is not None
    restored_policy.resume_statement(
        contract_for_run(resumed.current, resumed.index),
        instance_id=resumed.current_instance_id,
        history=journal.turns,
    )

    assert resumed.current_instance_id == instance_id
    assert resumed.notes_mark == 0
    assert restored_policy._rt.statement_info_emitted is True
    assert restored_policy._initial_filters == {"status": "open"}


def test_program_runtime_replays_abandoned_kickback_replacement():
    old_run = Run(statement_id="old", name="blocked route", kind="navigation")
    old_program = Program(goal="g", statements=[old_run])
    new_run = Run(statement_id="new", name="feasible route", kind="navigation")
    new_program = Program(goal="g", statements=[new_run, Finish(message="done")])
    journal = EventJournal()
    live = ProgramRuntime.start(old_program, journal=journal)
    instance_id = live.next_instance_id(old_run.statement_id)
    journal.append_statement_outcome(
        StatementOutcomeEvent(
            after_turn=0,
            observation_source="test",
            statement_instance_id=instance_id,
            statement_id=old_run.statement_id,
            statement_kind="navigation",
            outcome=StatementOutcome.infeasible(
                "route infeasible",
                kickback="use the feasible route",
            ),
        )
    )
    assert live.begin_kickback() == 1
    live.replace_program(
        new_program,
        reason="use the feasible route",
        terminal_disposition="abandon",
    )

    resumed = ProgramRuntime.resume(old_program, journal)

    assert resumed.program == new_program
    assert resumed.current is not None and resumed.current.name == "feasible route"
    assert resumed.interpreter.run_log == []
    assert resumed.begin_kickback() is None


def test_supervisor_step_without_reseed_raises():
    policy = StatementSupervisorPolicy()
    obs = Observation(png_bytes=b"x", source="test")
    with pytest.raises(RuntimeError, match="begin_statement"):
        policy.step(obs, "goal", [])


def test_supervisor_reseed_then_complete_does_not_walk_next_statement():
    """Single-statement executor: completing one statement does not open the next."""
    from gui_agent.core.run.execution_signals import CompletionEvaluation
    from gui_agent.core.schemas import StatementContract

    policy = StatementSupervisorPolicy()
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
    )
    step = policy._advance(
        first,
        [],
        decision=decision,
    )
    assert step.outcome is not None and step.outcome.phase == "completed"
    assert policy._active_statement is first
