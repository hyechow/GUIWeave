"""Statement lifecycle guard + kickback end_statement (Issue 7)."""
from gui_agent.core.orchestrator.program import Run
from gui_agent.core.run.interactive import contract_for_run
from gui_agent.core.supervisor.milestone.policy import MilestoneSupervisorPolicy


def _contract(sid="s1"):
    return contract_for_run(Run(statement_id=sid, name="x", kind="action"), 0)


def test_begin_statement_twice_raises():
    p = MilestoneSupervisorPolicy()
    p.begin_statement(_contract("s1"), instance_id="i1")
    try:
        import pytest
        with pytest.raises(RuntimeError, match="already active"):
            p.begin_statement(_contract("s2"), instance_id="i2")
    finally:
        p.end_statement()


def test_end_then_begin_succeeds():
    p = MilestoneSupervisorPolicy()
    p.begin_statement(_contract("s1"), instance_id="i1")
    p.end_statement()
    p.begin_statement(_contract("s2"), instance_id="i2")  # no raise
    assert p._statement_rt.instance_id == "i2"


def test_reset_for_return_retry_does_not_trip_guard():
    """reset_for_return_retry reuses the live runtime (does not call begin_statement), so the
    guard must not reject it."""
    p = MilestoneSupervisorPolicy()
    p.begin_statement(_contract("s1"), instance_id="i1")
    p.reset_for_return_retry(_contract("s1"))  # tightened, same instance
    assert p._statement_rt.instance_id == "i1"
    p.end_statement()
