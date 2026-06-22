"""Stage 3: the kick-back re-decompose guard + interpreter hot-swap mechanics.

The full loop is too heavy to drive in a unit test (browser/perception); these pin the deterministic
pieces the loop relies on — when a stop step is a re-plan (vs terminal), and that a re-decomposed
program drives cleanly via a fresh Interpreter (the hot-swap the loop performs)."""

from gui_agent.core.orchestrator import Interpreter
from gui_agent.core.orchestrator.program import Finish, Program, Run
from gui_agent.core.run.loop import MAX_KICKBACK_REPLANS, should_kickback_replan
from gui_agent.core.schemas import SupervisorStep


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
