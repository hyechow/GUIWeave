from gui_agent.core.schemas import (
    BaseAction,
    BaseActionDecision,
    StatementContract,
    Observation,
    PolicyTurn,
    SupervisorStep,
)
from gui_agent.core.supervisor.milestone import policy as P
from gui_agent.core.supervisor.milestone.policy import MilestoneSupervisorPolicy
from gui_agent.core.supervisor.milestone.schemas import _SingleCheckResult


def _price_milestone() -> StatementContract:
    return StatementContract(
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
    policy.begin_statement(milestone, instance_id="test:scope")

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


from gui_agent.core.supervisor.milestone.execution_scope import (
    execution_scope_for,
    history_for_scope,
)


def _inst_turn(
    index: int,
    *,
    instance_id: str,
    milestone_id: str = "price_action",
    execution_scope: str | None = None,
) -> PolicyTurn:
    return PolicyTurn(
        index=index,
        observation_source="browser",
        statement_instance_id=instance_id,
        supervisor=SupervisorStep(
            should_act=True, instruction=f"step {index}", summary="",
            milestone_id=milestone_id,
            execution_scope=execution_scope or f"{instance_id}/statement",
        ),
        executed=True,
    )


def test_history_for_scope_isolates_invocations_by_instance_id():
    """Two invocations of the same statement (same milestone_id, different instance_id) must
    get disjoint histories — the foreach/retry cross-talk Issue 3 targets."""
    milestone = _price_milestone()
    obs = Observation(png_bytes=b"png", source="browser")
    history = [
        _inst_turn(1, instance_id="i1"),
        _inst_turn(2, instance_id="i1"),
        _inst_turn(3, instance_id="i2"),  # a different invocation of the same statement
        _inst_turn(4, instance_id="i2"),
    ]

    inv1 = history_for_scope(history, milestone, obs, instance_id="i1")
    inv2 = history_for_scope(history, milestone, obs, instance_id="i2")

    assert [t.index for t in inv1] == [1, 2]
    assert [t.index for t in inv2] == [3, 4]


def test_one_invocation_keeps_resource_histories_disjoint():
    """A single call frame may visit several detail rows; resource identity stays a
    second isolation axis instead of being collapsed by instance id."""
    milestone = _price_milestone()
    first = Observation(
        png_bytes=b"png",
        source="browser",
        url="http://host/admin/catalog/product/edit/id/1841/",
    )
    second = Observation(
        png_bytes=b"png",
        source="browser",
        url="http://host/admin/catalog/product/edit/id/1842/",
    )
    first_scope = execution_scope_for(milestone, first, instance_id="i7")
    second_scope = execution_scope_for(milestone, second, instance_id="i7")
    history = [
        _inst_turn(1, instance_id="i7", execution_scope=first_scope),
        _inst_turn(2, instance_id="i7", execution_scope=second_scope),
    ]

    assert first_scope != second_scope
    assert [
        turn.index
        for turn in history_for_scope(history, milestone, second, instance_id="i7")
    ] == [2]
