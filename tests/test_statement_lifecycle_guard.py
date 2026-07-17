import pytest

from gui_agent.core.orchestrator import Interact
from gui_agent.core.orchestrator.runner import StatementInvocation
from gui_agent.core.run.interactive import contract_for_interact
from gui_agent.core.supervisor.statement.policy import StatementSupervisorPolicy


def _contract(sid="s1", goal="do one thing"):
    return contract_for_interact(
        StatementInvocation(
            statement=Interact(
                id=sid,
                goal=goal,
                success="the requested thing is done",
            )
        ),
        0,
    )


def test_begin_statement_twice_is_rejected_until_end():
    policy = StatementSupervisorPolicy()
    policy.begin_statement(_contract("s1"), instance_id="i1:s1")
    with pytest.raises(RuntimeError, match="active statement"):
        policy.begin_statement(_contract("s2"), instance_id="i2:s2")
    policy.end_statement()
    policy.begin_statement(_contract("s2"), instance_id="i2:s2")
    assert policy._statement_rt.instance_id == "i2:s2"


def test_return_retry_reuses_the_same_statement_instance():
    policy = StatementSupervisorPolicy()
    policy.begin_statement(_contract(), instance_id="i1:s1")
    policy.reset_for_return_retry(_contract(goal="continue reading output"))
    assert policy._statement_rt.instance_id == "i1:s1"
