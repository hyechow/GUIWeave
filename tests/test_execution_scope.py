from gui_agent.core.schemas import (
    BaseAction,
    BaseActionDecision,
    Milestone,
    Observation,
    PolicyTurn,
    SupervisorStep,
)
from gui_agent.core.supervisor.milestone import policy as P
from gui_agent.core.supervisor.milestone.policy import MilestoneSupervisorPolicy
from gui_agent.core.supervisor.milestone.schemas import _SingleCheckResult


def _price_milestone() -> Milestone:
    return Milestone(
        id="price_action",
        name="将价格更新为 64.88 并保存",
        description="d",
        success_condition="页面显示保存成功提示",
        kind="action",
    )


def _row_turn(index: int, product_id: str) -> PolicyTurn:
    action = BaseAction(
        action_type="type",
        x=540,
        y=548,
        text="64.88",
        description="在 Price 字段输入 64.88",
    )
    return PolicyTurn(
        index=index,
        observation_source="browser",
        supervisor=SupervisorStep(
            should_act=True,
            instruction="在 Price 字段输入 64.88",
            stop=False,
            goal_completed=False,
            summary="",
            milestone_id="price_action",
            execution_scope=f"row:admin/catalog/product/edit/id/{product_id}",
        ),
        action_decision=BaseActionDecision(action=action),
        executed=True,
    )


def test_checker_history_is_bucketed_by_current_row_scope(monkeypatch):
    policy = MilestoneSupervisorPolicy()
    milestone = _price_milestone()
    policy._milestones = {milestone.id: milestone}
    policy._order = [milestone.id]
    policy._current_id = milestone.id

    captured = {}

    def fake_single_check(_milestone, _observation, history, **_kwargs):
        captured["history"] = history
        return _SingleCheckResult(status="in_progress", effect_status="unverified", reason="还未保存", summary="进行中")

    monkeypatch.setattr(P, "is_loading_frame", lambda _obs: False)
    monkeypatch.setattr(policy, "_single_check", fake_single_check)
    monkeypatch.setattr(
        policy,
        "_plan_single",
        lambda *_args, **_kwargs: SupervisorStep(
            should_act=True,
            instruction="在 Price 字段输入 64.88",
            stop=False,
            goal_completed=False,
            summary="plan",
            milestone_id=milestone.id,
        ),
    )

    obs = Observation(
        png_bytes=b"png",
        source="browser",
        url="http://h:7780/admin/catalog/product/edit/id/1843/",
    )
    history = [_row_turn(1, "1841"), _row_turn(2, "1842")]

    policy._run_single_turn(milestone, obs, history)

    assert captured["history"] == []
