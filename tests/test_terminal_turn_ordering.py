"""Terminal observations persist the filled Outcome before runtime teardown."""

from gui_agent.core.orchestrator.program import Program, Run
from gui_agent.core.run.interactive import contract_for_run
from gui_agent.core.run.program_runtime import ProgramRuntime
from gui_agent.core.run.statements.outcome import StatementOutcome
from gui_agent.core.run.turns import (
    emit_statement_fields,
    interactive_turn_count,
    make_statement_outcome_event,
)
from gui_agent.core.schemas import PolicyContext, SupervisorStep
from gui_agent.core.supervisor.statement.policy import StatementSupervisorPolicy
from gui_agent.core.supervisor.statement.schemas import (
    _StatementTransitionResult,
    _TransitionEvidence,
)


def test_terminal_event_carries_live_transition_and_filled_outcome():
    policy = StatementSupervisorPolicy()
    contract = contract_for_run(
        Run(statement_id="s1", name="打开详情", kind="navigation", returns=["rating"]),
        0,
    )
    policy.begin_statement(contract, instance_id="i1:s1")
    policy._record_transition(
        _StatementTransitionResult(
            kind="complete",
            reason="已在目标页",
            summary="在目标页",
            evidence=[
                _TransitionEvidence(
                    source="current_observation",
                    claim="目标详情页当前可见",
                )
            ],
        ),
        [],
    )
    info, instance_id = emit_statement_fields(policy)
    filled = StatementOutcome.completed("done", reads={"rating": "5"})

    event = make_statement_outcome_event(
        after_turn=0,
        observation_source="test",
        supervisor_step=SupervisorStep(
            should_act=False,
            statement_id="s1",
            statement_kind="navigation",
            summary="done",
            outcome=StatementOutcome.completed("done"),
        ),
        supervisor=policy,
        statement=info,
        statement_instance_id=instance_id,
        outcome=filled,
    )

    assert event.outcome == filled
    assert event.transition is not None
    assert event.transition["proposal"]["kind"] == "complete"
    assert event.statement is not None and event.statement.id == "s1"
    assert event.statement_instance_id == "i1:s1"
    context = PolicyContext(
        goal="g",
        supervisor_policy_name="test",
        action_policy_name="test",
        journal={"events": [event]},
    )
    assert interactive_turn_count(context) == 0
    assert context.journal.turns == []


def test_exhausted_outcome_terminates_program_as_failure():
    runtime = ProgramRuntime.start(
        Program(statements=[Run(statement_id="s1", name="a", kind="action")]),
    )

    runtime.send_outcome(StatementOutcome.exhausted("合同未满足"))

    assert runtime.finished
    assert runtime.interpreter.run_log[-1].result.phase == "exhausted"
