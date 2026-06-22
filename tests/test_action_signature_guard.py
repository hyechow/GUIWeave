"""The supervisor's signature-based loop guard: re-typing the same value into the same box is caught
— url-INDEPENDENTLY, by counting the milestone's executed `type` signatures (not the planner's
wording, and not a canonical_url key).

The url-keyed version missed the very loop it was built for: a filter/search/reset cycle rewrites the
url's path shape (`/index/` → `/index/filter//internal_reviews/…`), so canonical_url is unstable
across the first-search boundary and the re-types landed under different states (regression
20260622_205544: 'Olivia zip jacket' typed 3× at T3/T6/T10, guard never fired). The signature alone
is the identity. Scoped to `type` (its value rides the signature); a re-click of Search/Reset is NOT
flagged here (the instruction guard owns those)."""

from gui_agent.adapters.browser.actions import BrowserActionDecision
from gui_agent.core.schemas import Milestone, Observation, PolicyTurn, SupervisorStep
from gui_agent.core.supervisor.milestone.policy import MilestoneSupervisorPolicy
from gui_agent.core.supervisor.milestone.schemas import _PlanResult, _SingleCheckResult

STUCK = "__STUCK_SENTINEL__"
# The two re-types happen on DIFFERENT urls (pre-filter vs post-reset) — the case the url-keyed guard
# missed. The guard must catch them anyway, on the signature alone.
URL_PREFILTER = "http://h:7780/admin/review/product/index/"
URL_POSTRESET = "http://h:7780/admin/review/product/index/filter//internal_reviews//form_key/xy/"
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


def _typed_turn(index: int) -> PolicyTurn:
    sv = SupervisorStep(should_act=True, instruction="在 Product 框输入 Olivia zip jacket", stop=False,
                        goal_completed=False, milestone_id="m1", summary="")
    ad = BrowserActionDecision.model_validate({"action": TYPE_ACTION})
    return PolicyTurn(index=index, observation_source="eval", supervisor=sv, action_decision=ad, executed=True)


def test_reworded_retype_is_caught_by_signature():
    # Two identical-signature type actions already executed in this milestone (across different urls).
    p = _policy("在 Product 列筛选框中删除现有内容并重新输入 'Olivia zip jacket'")  # different wording, same action
    history = [_typed_turn(3), _typed_turn(6)]
    obs = Observation(png_bytes=b"png", source="browser", url=URL_POSTRESET)
    check = _SingleCheckResult(status="in_progress", reason="筛选未生效", summary="进行中")
    step = p._plan_single(_ms(), check, obs, history)
    assert step.summary == STUCK  # routed to stuck despite the reworded instruction + churning url


def test_first_type_is_not_flagged():
    # Only ONE identical type executed → not yet a loop; the next attempt must go through.
    p = _policy("在 Product 列筛选框输入 'Olivia zip jacket'")
    obs = Observation(png_bytes=b"png", source="browser", url=URL_PREFILTER)
    check = _SingleCheckResult(status="in_progress", reason="筛选未生效", summary="进行中")
    step = p._plan_single(_ms(), check, obs, [_typed_turn(3)])
    assert step.summary != STUCK
    assert step.should_act
