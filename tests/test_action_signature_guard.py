"""The supervisor's signature-based loop guard: re-typing the same value into the same box is
caught even when the planner REWORDS the instruction — the gap that let WebArena run 20260622_171843
re-type 'Olivia zip jacket' at T3/T9/T12/T13 (instructions '输入X' → '删除后输入X' → '覆盖输入X') and
churn the Reviews grid until Feasibility fired 13 turns later.

Scoped to `type` (its value rides the signature); a re-click of Search/Reset is NOT flagged here."""

from gui_agent.adapters.browser.actions import BrowserActionDecision
from gui_agent.core.run.progress_monitor import action_signature, canonical_url
from gui_agent.core.schemas import Milestone, Observation, PolicyTurn, SupervisorStep
from gui_agent.core.supervisor.milestone.policy import MilestoneSupervisorPolicy
from gui_agent.core.supervisor.milestone.schemas import _PlanResult, _SingleCheckResult

STUCK = "__STUCK_SENTINEL__"
URL = "http://h:7780/admin/review/product/index/filter/abc==/internal_reviews//form_key/xy/"
TYPE_ACTION = {
    "action_type": "type", "x": 825, "y": 444, "text": "Olivia zip jacket",
    "description": "在 Product 列筛选框输入 Olivia zip jacket",
    "snap": {"snapped": [825, 444], "info": "input 61x28"},
}


def _policy(reworded_instruction: str) -> MilestoneSupervisorPolicy:
    p = MilestoneSupervisorPolicy()
    p._invoke_planner = lambda *a, **k: _PlanResult(instruction=reworded_instruction, summary="重输")  # type: ignore[method-assign]
    p._is_repeated_instruction = lambda *a, **k: False  # type: ignore[method-assign]  # instruction guard stays silent
    p._handle_stuck = lambda *a, **k: SupervisorStep(  # type: ignore[method-assign]
        should_act=False, stop=False, goal_completed=False, summary=STUCK,
    )
    return p


def _ms() -> Milestone:
    return Milestone.model_validate(
        {"id": "m1", "name": "筛选产品", "description": "d", "success_condition": "s", "kind": "action"}
    )


def _history_with_executed_type() -> list[PolicyTurn]:
    sv = SupervisorStep(should_act=True, instruction="在 Product 框输入 Olivia zip jacket", stop=False,
                        goal_completed=False, milestone_id="m1", summary="")
    ad = BrowserActionDecision.model_validate({"action": TYPE_ACTION})
    return [PolicyTurn(index=9, observation_source="eval", supervisor=sv, action_decision=ad, executed=True)]


def test_reworded_retype_is_caught_by_signature():
    p = _policy("在 Product 列筛选框中删除现有内容并重新输入 'Olivia zip jacket'")  # different wording, same action
    # an identical type was already executed earlier at this canonical state
    ad = BrowserActionDecision.model_validate({"action": TYPE_ACTION})
    p._monitor.note(3, canonical_url(URL), action_signature(ad.action))

    obs = Observation(png_bytes=b"png", source="browser", url=URL)
    check = _SingleCheckResult(status="in_progress", reason="筛选未生效", summary="进行中")
    step = p._plan_single(_ms(), check, obs, _history_with_executed_type())
    assert step.summary == STUCK  # routed to stuck despite the reworded instruction


def test_first_type_is_not_flagged():
    # No prior identical type recorded → the first attempt must go through (not a loop).
    p = _policy("在 Product 列筛选框输入 'Olivia zip jacket'")
    obs = Observation(png_bytes=b"png", source="browser", url=URL)
    check = _SingleCheckResult(status="in_progress", reason="筛选未生效", summary="进行中")
    step = p._plan_single(_ms(), check, obs, _history_with_executed_type())
    assert step.summary != STUCK
    assert step.should_act
