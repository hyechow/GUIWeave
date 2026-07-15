"""Stage 3: the kick-back re-decompose guard + interpreter hot-swap mechanics.

The full loop is too heavy to drive in a unit test (browser/perception); these pin the deterministic
pieces the loop relies on — when a stop step is a re-plan (vs terminal), and that a re-decomposed
program drives cleanly via a fresh Interpreter (the hot-swap the loop performs)."""

from gui_agent.core.orchestrator import Interpreter
from gui_agent.core.orchestrator.program import Finish, Program, Run
from gui_agent.core.orchestrator.runner import RunRecord
from gui_agent.core.run.recovery_router import RecoveryRouter
from gui_agent.core.run.result import orchestration_result
from gui_agent.core.schemas import PolicyContext, StatementOutcome


def _outcome(directive: str) -> StatementOutcome:
    return StatementOutcome.infeasible("blocked", kickback=directive)


def test_guard_true_when_all_conditions_met():
    decision = RecoveryRouter.route_statement(
        _outcome("drill"),
        can_redecompose=True,
    )
    assert decision.action == "kickback"
    assert decision.recovery_class == "infeasible_route"


def test_guard_false_without_directive():
    decision = RecoveryRouter.route_statement(
        StatementOutcome.failed("blocked"),
        can_redecompose=True,
    )
    assert decision.action == "fail_or_escalate"


def test_guard_false_without_redecompose_callable():
    decision = RecoveryRouter.route_statement(
        _outcome("drill"),
        can_redecompose=False,
    )
    assert decision.action == "fail_or_escalate"


def test_router_exposes_return_and_program_end_recovery_without_state():
    router = RecoveryRouter()
    assert vars(router) == {}
    assert router.route_statement(
        StatementOutcome.completed("done"),
        return_violation=True,
    ).action == "tighten_return"
    assert router.route_statement(
        StatementOutcome.completed("done"),
    ).action == "advance_program"
    decision = router.route_program_end(
        failure_evidence="wrong data source",
        can_redecompose=True,
    )
    assert decision.action == "kickback"
    assert decision.recovery_class == "data_source_error"


def test_redecomposed_program_drives_via_fresh_interpreter():
    # The loop's hot-swap: redecompose() → new Program → Interpreter(new).steps() primed to run 1.
    new = Program(goal="g", statements=[
        Run(name="逐条钻取每条评论详情读评分", kind="navigation", success_condition="进入评论详情"),
        Finish(message="done"),
    ])
    gen = Interpreter(new).steps()
    first = next(gen)
    assert first.name == "逐条钻取每条评论详情读评分"


def _rec(name: str, *, failed: bool) -> RunRecord:
    return RunRecord(
        name=name, var=None,
        result=(StatementOutcome.failed(name) if failed else StatementOutcome.completed(name)),
    )


def test_recovered_kickback_does_not_inherit_superseded_failure(monkeypatch):
    """A kickback re-plans because a re-plannable step failed; the loop hot-swaps to an interpreter that
    inherits the prior run_log. If that inherited log keeps the superseded ✗ record, `interp.failed`
    stays True forever and `orchestration_result` reports phase=failed even though the
    re-decompose recovered and finish produced the answer. The loop now drops failed records from
    the inherited run_log."""
    prev_log = [
        _rec("进入记录列表", failed=False),
        _rec("按目标字段筛选记录", failed=False),
        _rec("查询符合条件的记录", failed=True),
    ]

    # WITHOUT the fix: inheriting the raw log keeps the ✗ → failed=True → phase=failed.
    poisoned = Interpreter(Program(goal="g", statements=[Finish(message="done")]))
    poisoned.run_log = list(prev_log)
    assert poisoned.failed is True

    # WITH the fix: the loop filters failed records before inheritance.
    recovered = Interpreter(Program(goal="g", statements=[Finish(message="done")]))
    recovered.run_log = [r for r in prev_log if r.result.is_completed]
    recovered.run_log.append(_rec("逐条读取后查询符合条件的记录", failed=False))
    assert recovered.failed is False

    ctx = PolicyContext(goal="g", supervisor_policy_name="statement", action_policy_name="action")
    monkeypatch.setattr(
        "gui_agent.core.llm.output.compose_orchestration_reply",
        lambda *_args, **_kwargs: "completed",
    )
    result = orchestration_result(ctx, recovered, "完成", current=None)
    assert result.phase == "completed"
    assert result.verification == "confirmed"
