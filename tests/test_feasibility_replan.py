"""Stage 3: the kick-back re-decompose guard + interpreter hot-swap mechanics.

The full loop is too heavy to drive in a unit test (browser/perception); these pin the deterministic
pieces the loop relies on — when a stop step is a re-plan (vs terminal), and that a re-decomposed
program drives cleanly via a fresh Interpreter (the hot-swap the loop performs)."""

from gui_agent.core.orchestrator import Interpreter
from gui_agent.core.orchestrator.program import Finish, Program, Run
from gui_agent.core.orchestrator.runner import RunRecord, RunResult
from gui_agent.core.orchestrator.callframe import MAX_KICKBACK_REPLANS, should_kickback_replan
from gui_agent.core.run.result import orchestration_result
from gui_agent.core.schemas import PolicyContext, SupervisorStep


def _step(directive=None) -> SupervisorStep:
    return SupervisorStep(
        should_act=False, stop=True, goal_completed=False, summary="",
        replan_directive=directive,
    )


def _noop_redecompose(_d):
    return None


def test_guard_true_when_all_conditions_met():
    assert should_kickback_replan(_step("drill"), object(), _noop_redecompose, 0) is True


def test_guard_false_without_directive():
    assert should_kickback_replan(_step(None), object(), _noop_redecompose, 0) is False


def test_guard_false_in_dag_mode_no_program():
    assert should_kickback_replan(_step("drill"), None, _noop_redecompose, 0) is False


def test_guard_false_without_redecompose_callable():
    assert should_kickback_replan(_step("drill"), object(), None, 0) is False


def test_guard_false_when_budget_spent():
    assert should_kickback_replan(_step("drill"), object(), _noop_redecompose, MAX_KICKBACK_REPLANS) is False


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
        result=RunResult(completed=not failed, failed=failed, summary=name),
    )


def test_recovered_kickback_does_not_inherit_superseded_failure():
    """Regression (task-113 live run 20260625_151340 scored 0 with the right answer): a kickback
    re-plans *because* a re-plannable step failed; the loop hot-swaps to a fresh Interpreter that
    inherits the prior run_log. If that inherited log keeps the superseded ✗ record, `interp.failed`
    stays True forever and `orchestration_result` reports goal_completed=False even though the
    re-decompose recovered and finish produced the answer. The loop now drops failed records from
    the inherited run_log (loop.py: `[r for r in _prev_log if not r.result.failed]`)."""
    prev_log = [
        _rec("进入 All Reviews 页面", failed=False),
        _rec("用 Product 列筛选评论", failed=False),
        _rec("筛出评分<=3 的评论者昵称", failed=True),  # the data_query that triggered the kickback
    ]

    # WITHOUT the fix: inheriting the raw log keeps the ✗ → failed=True → goal_completed=False.
    poisoned = Interpreter(Program(goal="g", statements=[Finish(message="done")]))
    poisoned.run_log = list(prev_log)
    assert poisoned.failed is True

    # WITH the fix: the loop filters failed records before inheritance.
    recovered = Interpreter(Program(goal="g", statements=[Finish(message="done")]))
    recovered.run_log = [r for r in prev_log if not r.result.failed]
    recovered.run_log.append(_rec("逐条钻取后筛出评分<=3 的评论者昵称", failed=False))  # redecompose's success
    assert recovered.failed is False

    ctx = PolicyContext(goal="g", supervisor_policy_name="milestone", action_policy_name="action")
    result = orchestration_result(ctx, recovered, "完成", current=None)
    assert result["goal_completed"] is True
