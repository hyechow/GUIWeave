"""Terminal observations persist the filled Outcome before runtime teardown."""

from gui_agent.core.orchestrator.program import Program, Run
from gui_agent.core.run.interactive import contract_for_run
from gui_agent.core.run.program_runtime import ProgramRuntime
from gui_agent.core.run.statements.outcome import StatementOutcome
from gui_agent.core.run.turns import emit_statement_fields, interactive_turn_count, make_verdict_turn
from gui_agent.core.schemas import PolicyContext, SupervisorStep
from gui_agent.core.supervisor.milestone.policy import MilestoneSupervisorPolicy
from gui_agent.core.supervisor.milestone.schemas import _SingleCheckResult


def test_terminal_observation_carries_live_checker_and_filled_outcome():
    policy = MilestoneSupervisorPolicy()
    contract = contract_for_run(
        Run(statement_id="s1", name="打开详情", kind="navigation", returns=["rating"]),
        0,
    )
    policy.begin_statement(contract, instance_id="i1:s1")
    policy._statement_rt.last_check = _SingleCheckResult(
        status="done",
        reason="已在目标页",
        summary="在目标页",
        effect_status="confirmed",
    )
    info, instance_id = emit_statement_fields(policy)
    filled = StatementOutcome.completed("done", reads={"rating": "5"})

    turn = make_verdict_turn(
        index=1,
        observation_source="test",
        supervisor_step=SupervisorStep(
            should_act=False,
            milestone_id="s1",
            summary="done",
            outcome=StatementOutcome.completed("done"),
        ),
        supervisor=policy,
        observation_only=True,
        statement=info,
        statement_instance_id=instance_id,
        outcome_override=filled,
    )

    assert turn.supervisor.outcome == filled
    assert turn.checker is not None and turn.checker["status"] == "done"
    assert turn.statement is not None and turn.statement.id == "s1"
    assert turn.statement_instance_id == "i1:s1"
    context = PolicyContext(
        goal="g",
        supervisor_policy_name="test",
        action_policy_name="test",
        journal={"events": [turn]},
    )
    assert interactive_turn_count(context) == 0


def test_exhausted_outcome_terminates_program_as_failure():
    runtime = ProgramRuntime.start(
        Program(statements=[Run(statement_id="s1", name="a", kind="action")]),
    )

    runtime.send_outcome(StatementOutcome.exhausted("合同未满足"))

    assert runtime.finished
    assert runtime.interpreter.run_log[-1].result.phase == "exhausted"
