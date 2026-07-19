from gui_agent.core.run.recovery_router import RecoveryRouter
from gui_agent.core.schemas import StatementOutcome


def test_only_explicit_infeasible_kickback_recompiles_program():
    infeasible = StatementOutcome.infeasible(
        "route cannot satisfy statement",
        kickback="choose a different semantic route",
    )
    assert RecoveryRouter.route_statement(
        infeasible, can_redecompose=True
    ).action == "kickback"
    for outcome in (
        StatementOutcome.failed("dispatch failed"),
        StatementOutcome.exhausted("statement replanning exhausted"),
    ):
        assert RecoveryRouter.route_statement(
            outcome, can_redecompose=True
        ).action == "fail_or_escalate"


def test_guard_rejection_is_not_itself_a_program_recompile_signal():
    assert RecoveryRouter.route_program_end(
        replan_directive=None,
        can_redecompose=True,
    ).action == "fail_or_escalate"
    assert RecoveryRouter.route_program_end(
        replan_directive="the statement is semantically infeasible",
        can_redecompose=True,
    ).action == "kickback"


def test_completed_statement_advances_without_interact_return_tightening():
    completed = StatementOutcome.completed("done")
    assert RecoveryRouter.route_statement(completed).action == "advance_program"
