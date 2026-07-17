from gui_agent.core.orchestrator import Interact, OutputSpec
from gui_agent.core.orchestrator.runner import StatementInvocation
from gui_agent.core.run.interactive import contract_for_interact
from gui_agent.core.run.turns import emit_statement_fields
from gui_agent.core.supervisor.statement.policy import StatementSupervisorPolicy


def _contract(goal="open the current record"):
    invocation = StatementInvocation(
        statement=Interact(
            id="s1",
            goal=goal,
            success="the current record detail is visible",
            returns={"rating": OutputSpec(type="number")},
        )
    )
    return contract_for_interact(invocation, 0)


def test_statement_info_is_emitted_once_per_invocation():
    policy = StatementSupervisorPolicy()
    policy.begin_statement(_contract(), instance_id="i9:s1")

    first_info, first_id = emit_statement_fields(policy)
    next_info, next_id = emit_statement_fields(policy)

    assert first_info is not None and first_info.id == "s1"
    assert next_info is None
    assert first_id == next_id == "i9:s1"


def test_return_retry_keeps_invocation_and_does_not_reemit_contract():
    policy = StatementSupervisorPolicy()
    policy.begin_statement(_contract(), instance_id="i9:s1")
    emit_statement_fields(policy)

    policy.reset_for_return_retry(_contract("continue locating rating"))

    assert emit_statement_fields(policy) == (None, "i9:s1")
