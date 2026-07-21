import pytest

from gui_agent.core.orchestrator import Acquire, Read, Finish, Interact, OutputSpec, Program
from gui_agent.core.run.program_runtime import ProgramRuntime
from gui_agent.core.schemas import (
    AcquisitionReceiptEvent,
    EventJournal,
    StatementOutcome,
    StatementOutcomeEvent,
)


def _program() -> Program:
    return Program(
        goal="two steps",
        statements=[
            Read(
                id="derive",
                bind="derived",
                returns={"value": OutputSpec(type="text")},
            ),
            Interact(
                id="apply",
                goal="apply derived value",
                success="the derived value is applied",
                inputs={"value": {"var": "derived", "path": ["value"]}},
            ),
            Finish(message="done"),
        ],
    )


def test_runtime_is_the_only_owner_of_program_cursor_and_env():
    runtime = ProgramRuntime.start(_program())
    assert runtime.current is not None
    assert runtime.current.id == "derive"
    iid = runtime.next_instance_id("derive")

    runtime.send_outcome(
        StatementOutcome.completed("derived", outputs={"value": "ready"})
    )

    assert iid == "i1:derive"
    assert runtime.current is not None
    assert runtime.current.id == "apply"
    assert runtime.current.inputs == {"value": "ready"}
    assert runtime.interpreter.env == {"derived": {"value": "ready"}}
    assert runtime.interpreter.run_log[0].instance_id == iid


def test_runtime_replays_new_program_revision_and_outcome_events():
    journal = EventJournal()
    runtime = ProgramRuntime.start(_program(), journal=journal)
    iid = runtime.next_instance_id("derive")
    outcome = StatementOutcome.completed("derived", outputs={"value": "ready"})
    journal.append_statement_outcome(
        StatementOutcomeEvent(
            after_turn=0,
            statement_instance_id=iid,
            statement_id="derive",
            outcome=outcome,
        )
    )
    runtime.send_outcome(outcome)

    resumed = ProgramRuntime.resume(_program(), journal)

    assert resumed.current is not None
    assert resumed.current.id == "apply"
    assert resumed.current.inputs == {"value": "ready"}
    assert resumed.interpreter.run_log[0].instance_id == iid


def test_runtime_rejects_journal_without_initial_program_revision():
    journal = EventJournal()
    journal.append_statement_outcome(
        StatementOutcomeEvent(
            after_turn=0,
            statement_instance_id="i1:derive",
            statement_id="derive",
            outcome=StatementOutcome.completed(
                "derived", outputs={"value": "ready"}
            ),
        )
    )
    with pytest.raises(ValueError):
        ProgramRuntime.resume(_program(), journal)


def test_runtime_recovers_active_acquire_from_journal_receipt():
    program = Program(
        goal="collect",
        statements=[
            Acquire(
                id="collect",
                bind="collected",
                goal="collect the scoped records",
                returns={"rows": OutputSpec(type="list[record]", coverage="complete")},
            ),
            Finish(message="done"),
        ],
    )
    journal = EventJournal()
    runtime = ProgramRuntime.start(program, journal=journal)
    acquire_iid = runtime.next_instance_id("collect")
    journal.append_acquisition_receipt(AcquisitionReceiptEvent(
        event_ref="acquire:1",
        after_turn=0,
        statement_instance_id=acquire_iid,
        statement_id="collect",
        strategy="structured",
        capability="bind:structured",
        action_family="bind_region",
        status="selected",
        bound_region="table:grid",
    ))

    resumed = ProgramRuntime.resume(program, journal)

    assert resumed.current is not None and resumed.current.id == "collect"
    assert resumed.current_instance_id == acquire_iid


def test_runtime_requires_one_active_statement_instance_at_a_time():
    runtime = ProgramRuntime.start(_program())
    runtime.next_instance_id("derive")
    with pytest.raises(RuntimeError):
        runtime.next_instance_id("derive")
