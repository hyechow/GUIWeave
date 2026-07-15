"""Interactive turns expose one contract snapshot and a stable invocation id."""

from gui_agent.core.orchestrator.program import Run
from gui_agent.core.run.interactive import contract_for_run
from gui_agent.core.run.turns import emit_statement_fields
from gui_agent.core.supervisor.statement.policy import StatementSupervisorPolicy


def _contract(name: str = "打开详情"):
    return contract_for_run(
        Run(statement_id="s1", name=name, kind="navigation", returns=["rating"]),
        0,
    )


def test_statement_info_is_emitted_once_per_invocation():
    policy = StatementSupervisorPolicy()
    policy.begin_statement(_contract(), instance_id="i9:s1")

    first_info, first_id = emit_statement_fields(policy)
    next_info, next_id = emit_statement_fields(policy)

    assert first_info is not None and first_info.id == "s1"
    assert next_info is None
    assert first_id == next_id == "i9:s1"


def test_return_tighten_keeps_invocation_and_does_not_reemit_contract():
    policy = StatementSupervisorPolicy()
    policy.begin_statement(_contract(), instance_id="i9:s1")
    emit_statement_fields(policy)

    policy.reset_for_return_retry(_contract("继续定位 rating"))

    assert emit_statement_fields(policy) == (None, "i9:s1")
