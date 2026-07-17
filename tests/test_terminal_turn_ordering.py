from gui_agent.core.orchestrator import Interact, Program
from gui_agent.core.orchestrator.runner import StatementInvocation
from gui_agent.core.run.interactive import contract_for_interact
from gui_agent.core.run.program_runtime import ProgramRuntime
from gui_agent.core.run.turns import (
    emit_statement_fields,
    interactive_turn_count,
    make_statement_outcome_event,
)
from gui_agent.core.schemas import PolicyContext, StatementOutcome, SupervisorStep
from gui_agent.core.supervisor.statement.policy import StatementSupervisorPolicy
from gui_agent.core.supervisor.statement.schemas import (
    _StatementTransitionResult,
    _TransitionAssessment,
    _TransitionEvidence,
)


def test_terminal_event_is_recorded_separately_from_action_turns():
    invocation = StatementInvocation(
        statement=Interact(
            id="s1",
            goal="open detail",
            success="the target detail is visible",
        )
    )
    policy = StatementSupervisorPolicy()
    policy.begin_statement(contract_for_interact(invocation, 0), instance_id="i1:s1")
    policy._record_transition(
        _StatementTransitionResult(
            assessment=_TransitionAssessment(
                status="satisfied",
                summary="target detail is visible",
            ),
            kind="complete",
            reason="done",
            evidence=[
                _TransitionEvidence(
                    source="current_observation",
                    claim="target detail is visible",
                )
            ],
        )
    )
    info, instance_id = emit_statement_fields(policy)
    filled = StatementOutcome.completed("done", outputs={"rating": 5})
    event = make_statement_outcome_event(
        after_turn=0,
        observation_source="browser",
        supervisor_step=SupervisorStep(
            statement_id="s1",
            summary="done",
            outcome=StatementOutcome.completed("done"),
        ),
        supervisor=policy,
        statement=info,
        statement_instance_id=instance_id,
        outcome=filled,
    )
    context = PolicyContext(
        goal="g",
        supervisor_policy_name="statement",
        action_policy_name="test",
        journal={"events": [event]},
    )

    assert event.outcome == filled
    assert event.transition["proposal"]["kind"] == "complete"
    assert interactive_turn_count(context) == 0
    assert context.journal.turns == []


def test_exhausted_outcome_terminates_program_as_failure():
    runtime = ProgramRuntime.start(
        Program(
            statements=[
                Interact(id="s1", goal="do task", success="task done")
            ]
        )
    )
    runtime.send_outcome(StatementOutcome.exhausted("contract not satisfied"))
    assert runtime.finished
    assert runtime.interpreter.run_log[-1].result.phase == "exhausted"
