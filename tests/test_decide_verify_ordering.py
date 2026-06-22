"""Verify-first ordering in the single-step supervisor.

Regression for logs/.../android/20260611_085000: turn 2 hit the 闹钟 tab and the screen
DID advance to the alarm page, but TargetVerify false-flagged off-target (and settle
false-flagged no-effect on a 6.7<8.0 whole-frame diff). The OffTarget / NoEffect fast-paths
used to run BEFORE _single_check, so turn 3 skipped verification and replanned a milestone
the action had already satisfied.

Fix: run _single_check FIRST — if done, advance; only when NOT done do off-target /
no-effect route to replan. These lock that ordering by mocking the checker.
"""

from __future__ import annotations

from gui_agent.core.supervisor.milestone import policy as P
from gui_agent.core.supervisor.milestone.policy import MilestoneSupervisorPolicy
from gui_agent.core.supervisor.milestone.schemas import _SingleCheckResult
from gui_agent.core.schemas import (
    BaseAction,
    BaseActionDecision,
    Milestone,
    Observation,
    PolicyTurn,
    SupervisorStep,
    TargetVerify,
)


def _policy():
    p = MilestoneSupervisorPolicy()
    m = Milestone.model_validate(
        {"id": "m1", "name": "进入闹钟页", "description": "d", "success_condition": "闹钟列表页", "kind": "action"}
    )
    p._milestones = {"m1": m}
    p._current_id = "m1"
    p._order = ["m1"]
    return p, m


def _tap_turn(*, on_target: bool, no_effect: bool = False) -> PolicyTurn:
    act = BaseAction(action_type="tap", x=125, y=967, description="点击底部『闹钟』tab")
    return PolicyTurn(
        index=1,
        observation_source="test",
        supervisor=SupervisorStep(
            should_act=True, instruction="点击底部『闹钟』tab", stop=False,
            goal_completed=False, summary="", milestone_id="m1",
        ),
        action_decision=BaseActionDecision(action=act),
        target_verify=TargetVerify(on_target=on_target, actual_element="世界时钟 tab"),
        no_effect=no_effect,
        executed=True,
    )


def _wire(monkeypatch, p, check_status: str) -> list[str]:
    """Mock the LLM checker + the two terminal branches; record which fired."""
    monkeypatch.setattr(P, "is_loading_frame", lambda obs: False)
    monkeypatch.setattr(
        p, "_single_check",
        lambda *a, **k: _SingleCheckResult(status=check_status, reason="r", summary="s"),
    )
    calls: list[str] = []
    monkeypatch.setattr(p, "_advance", lambda *a, **k: (calls.append("advance"), "ADV")[1])
    monkeypatch.setattr(p, "_handle_stuck", lambda *a, **k: (calls.append("stuck"), "STK")[1])
    return calls


def test_done_advances_despite_false_off_target(monkeypatch):
    p, m = _policy()
    calls = _wire(monkeypatch, p, "done")
    p._run_single_turn(m, Observation(png_bytes=b"x", source="test"), [_tap_turn(on_target=False)])
    assert calls == ["advance"]  # verified first -> advanced, NOT replanned


def test_off_target_still_replans_when_not_done(monkeypatch):
    p, m = _policy()
    calls = _wire(monkeypatch, p, "in_progress")
    p._run_single_turn(m, Observation(png_bytes=b"x", source="test"), [_tap_turn(on_target=False)])
    assert calls == ["stuck"]  # not done -> off-target signal still routes to replan


def test_done_advances_despite_false_no_effect(monkeypatch):
    p, m = _policy()
    calls = _wire(monkeypatch, p, "done")
    p._run_single_turn(
        m, Observation(png_bytes=b"x", source="test"),
        [_tap_turn(on_target=True, no_effect=True)],
    )
    assert calls == ["advance"]  # false no_effect no longer blocks verification


# ── URL-change (browser ground truth) suppresses pixel false positives ──────────
def _wire_plan(monkeypatch, p) -> list[str]:
    """Like _wire but in_progress + also records _plan_single / suppresses repetition."""
    calls = _wire(monkeypatch, p, "in_progress")
    monkeypatch.setattr(p, "_plan_single", lambda *a, **k: (calls.append("plan"), "PLAN")[1])
    monkeypatch.setattr(p._monitor, "check_instruction_repetition", lambda *a, **k: None)
    return calls


def test_url_change_suppresses_false_no_effect(monkeypatch):
    p, m = _policy()
    calls = _wire_plan(monkeypatch, p)
    p._monitor._last_url = "http://x/orders"
    obs = Observation(png_bytes=b"x", source="browser", url="http://x/orders/transport")  # navigated
    p._run_single_turn(m, obs, [_tap_turn(on_target=True, no_effect=True)])
    assert calls == ["plan"]  # URL changed => the tap DID navigate => no_effect suppressed


def test_no_url_change_keeps_no_effect_replan(monkeypatch):
    p, m = _policy()
    calls = _wire_plan(monkeypatch, p)
    p._monitor._last_url = "http://x/orders"
    obs = Observation(png_bytes=b"x", source="browser", url="http://x/orders")  # unchanged
    p._run_single_turn(m, obs, [_tap_turn(on_target=True, no_effect=True)])
    assert calls == ["stuck"]  # URL unchanged => no_effect stands => replan


def test_checker_stuck_status_routes_to_handle_stuck(monkeypatch):
    # The checker's OWN PROGRESS verdict (status=stuck, judged from the task-progress trace) routes
    # to the stuck path — before the deterministic off-target / no-effect signals.
    p, m = _policy()
    calls = _wire(monkeypatch, p, "stuck")
    p._run_single_turn(m, Observation(png_bytes=b"x", source="test"), [_tap_turn(on_target=True)])
    assert calls == ["stuck"]
