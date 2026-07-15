from __future__ import annotations

from types import SimpleNamespace

from gui_agent.core.run import turns
from gui_agent.core.schemas import CollectionScope, PolicyContext, SupervisorStep


def _context() -> PolicyContext:
    return PolicyContext(
        goal="g",
        supervisor_policy_name="milestone",
        action_policy_name="browser",
    )


def _step(scope: CollectionScope | None = None) -> SupervisorStep:
    return SupervisorStep(
        should_act=False,
        stop=False,
        goal_completed=False,
        summary="s",
        collection_scope=scope,
    )


def test_sync_turn_metadata_records_models_milestones_and_task_type(monkeypatch):
    ctx = _context()
    supervisor = SimpleNamespace(
        task_type="analysis",
        _milestones={
            "m1": SimpleNamespace(
                id="m1",
                name="打开订单",
                description="进入订单列表",
                kind="navigation",
                success_condition="显示订单列表",
            )
        },
    )
    monkeypatch.setattr(
        turns,
        "resolve_llm_config",
        lambda key: SimpleNamespace(model=f"model:{key}"),
    )
    messages = []

    turns.sync_turn_metadata(
        context=ctx,
        supervisor=supervisor,
        sv_step=_step(),
        program=None,
        say=messages.append,
    )

    assert ctx.models["supervisor"] == "model:supervisor"
    assert ctx.models["recon.navigator"] == "model:recon.navigator"
    assert ctx.milestones == []
    assert ctx.task_type == "analysis"
    assert messages == ["任务类型: analysis"]


def test_sync_turn_metadata_updates_collection_scope(monkeypatch):
    ctx = _context()
    monkeypatch.setattr(turns, "resolve_llm_config", lambda key: SimpleNamespace(model="m"))
    scope = CollectionScope(label="订单日期", start="01/01/2022", end="12/31/2023")
    messages = []

    turns.sync_turn_metadata(
        context=ctx,
        supervisor=SimpleNamespace(),
        sv_step=_step(scope),
        program=object(),
        say=messages.append,
    )

    assert ctx.collection_scope == scope
    assert messages == [
        '采集范围: {"label": "订单日期", "start": "01/01/2022", "end": "12/31/2023", "evidence": []}'
    ]
