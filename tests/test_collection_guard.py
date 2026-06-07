"""Unit test: a collection (scroll_until_boundary) milestone must never finish with
zero successful scrolls.

Regression — session 20260607_100322: the filtered bill list rendered (one screen,
but actually scrollable), the collection milestone was entered fresh, loop_check said
should_stop, and because nothing was collected it went stuck → replan → force_complete
(pre_existing=True) WITHOUT ever scrolling. content_notes ended empty and the output
hallucinated a wrong total (456.80 < the visible sum alone). Guard: a should_stop on
the entry frame (no scroll yet) is not trusted — force one scroll-collect first; only
after a real scroll may a still-empty collection go stuck.

No LLM: _loop_check / _invoke_loop_scroll / _handle_stuck are stubbed.
"""

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

from policy_expr.schemas import (
    Action,
    ActionDecision,
    Milestone,
    Observation,
    PolicyTurn,
    SupervisorStep,
)
from policy_expr.supervisor.milestone import MilestoneSupervisorPolicy
from policy_expr.supervisor.milestone.schemas import _LoopFrameResult, _PlanResult

passed = 0
failed = 0


def _report(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    passed += ok
    failed += not ok
    tag = "PASS" if ok else "FAIL"
    line = f"  [{tag}] {label:48s}"
    if detail:
        line += f"  {detail}"
    print(line)


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 80), (200, 200, 200)).save(buf, format="PNG")
    return buf.getvalue()


def _make_policy() -> tuple[MilestoneSupervisorPolicy, Milestone]:
    p = MilestoneSupervisorPolicy()
    ms = Milestone(
        id="5",
        name="采集账单明细",
        description="滚动采集指定区间的账单明细",
        success_condition="账单列表已滚动至物理底部",
        kind="collection",
        completion_strategy="scroll_until_boundary",
    )
    p._milestones = {"5": ms}
    p._order = ["5"]
    p._current_id = "5"
    p.task_type = "analysis"
    return p, ms


def _stub_loop(p: MilestoneSupervisorPolicy, *, should_stop: bool, boundary: bool = False) -> None:
    p._loop_check = lambda *a, **k: _LoopFrameResult(  # type: ignore[assignment]
        should_stop=should_stop, boundary_reached=boundary,
        stop_reason="看起来已到底", read_instruction="读取账单明细", summary="账单列表",
    )
    p._invoke_loop_scroll = lambda *a, **k: _PlanResult(  # type: ignore[assignment]
        instruction="向上滚动账单列表采集下一屏", summary="继续滚动采集",
    )


def _scroll_turn(mid: str, *, read_added: bool) -> PolicyTurn:
    return PolicyTurn(
        index=1,
        observation_source="eval",
        supervisor=SupervisorStep(
            should_act=True, instruction="向上滚动", stop=False,
            goal_completed=False, summary="", milestone_id=mid,
        ),
        action_decision=ActionDecision(
            action=Action(
                action_type="scroll", direction="up",
                target_area="main_content", description="向上滚动",
            ),
        ),
        executed=True,
        read_added_content=read_added,
    )


def main() -> int:
    print("── Collection Guard Unit ──")
    obs = Observation(png_bytes=_png(), source="eval")

    # Case 1: fresh entry (no history), loop_check says should_stop → must NOT finish;
    # must plan a scroll instead (force one scroll-collect before trusting "to the bottom").
    p, ms = _make_policy()
    _stub_loop(p, should_stop=True)
    step = p._run_loop_turn(ms, obs, [])
    ok = (
        step.should_act
        and not step.stop
        and not step.goal_completed
        and ms.status != "done"
    )
    _report(
        "零滚动+should_stop → 强制滚动、不判完成",
        ok,
        f"should_act={step.should_act} stop={step.stop} gc={step.goal_completed} status={ms.status}",
    )

    # Case 2: same, but boundary_reached=True on entry (still no scroll) → still must scroll,
    # because line 526 boundary-advance requires a prior scroll (_last_scroll_was_for).
    p, ms = _make_policy()
    _stub_loop(p, should_stop=False, boundary=True)
    step = p._run_loop_turn(ms, obs, [])
    ok = step.should_act and not step.stop and ms.status != "done"
    _report(
        "零滚动+boundary_reached → 强制滚动、不判完成",
        ok,
        f"should_act={step.should_act} status={ms.status}",
    )

    # Case 3: HAS a successful scroll already but should_stop + nothing collected →
    # the genuine-stuck path must still fire (route to _handle_stuck).
    p, ms = _make_policy()
    _stub_loop(p, should_stop=True)
    hit = {"stuck": False}

    def _fake_stuck(*a, **k):
        hit["stuck"] = True
        return SupervisorStep(
            should_act=False, stop=False, goal_completed=False,
            summary="stuck-sentinel", milestone_id="5",
        )

    p._handle_stuck = _fake_stuck  # type: ignore[assignment]
    p._run_loop_turn(ms, obs, [_scroll_turn("5", read_added=False)])
    _report(
        "已滚动+should_stop+零采集 → 仍走 stuck",
        hit["stuck"],
        f"handle_stuck_called={hit['stuck']}",
    )

    # Case 4: should_stop AND already collected → normal finish (advance, status=done).
    p, ms = _make_policy()
    _stub_loop(p, should_stop=True)
    step = p._run_loop_turn(ms, obs, [_scroll_turn("5", read_added=True)])
    ok = ms.status == "done"
    _report(
        "已采集+should_stop → 正常结束收集（advance）",
        ok,
        f"status={ms.status} stop={step.stop}",
    )

    print(f"\n{passed + failed} tests: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


def test_collection_guard() -> None:
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
