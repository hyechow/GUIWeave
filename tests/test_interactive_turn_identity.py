from gui_agent.core.run.contracts import Interact, StatementInvocation
from gui_agent.core.run.interactive import contract_for_interact
from gui_agent.core.run.turns import emit_statement_fields
from gui_agent.core.supervisor.statement.policy import StatementSupervisorPolicy


def _contract(goal="open the current record"):
    invocation = StatementInvocation(
        statement=Interact(
            id="s1",
            goal=goal,
            success="the current record detail is visible",
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


def test_new_invocation_emits_its_own_contract_once():
    policy = StatementSupervisorPolicy()
    policy.begin_statement(_contract(), instance_id="i9:s1")
    emit_statement_fields(policy)
    policy.end_statement()
    policy.begin_statement(_contract("open another record"), instance_id="i10:s1")
    info, instance_id = emit_statement_fields(policy)
    assert info is not None and info.goal == "open another record"
    assert instance_id == "i10:s1"
