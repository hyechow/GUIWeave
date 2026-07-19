from __future__ import annotations

from types import SimpleNamespace

from gui_agent.core.run import turns
from gui_agent.core.schemas import CollectionScope, PolicyContext, SupervisorStep


def _context() -> PolicyContext:
    return PolicyContext(
        goal="g",
        supervisor_policy_name="statement",
        action_policy_name="browser",
    )


def _step(scope: CollectionScope | None = None) -> SupervisorStep:
    return SupervisorStep(summary='s', collection_scope=scope)


def test_sync_turn_metadata_records_models_statements_and_task_type(monkeypatch):
    ctx = _context()
    supervisor = SimpleNamespace(
        task_type="analysis",
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
    assert ctx.models["data"] == "model:data"
    assert ctx.models["recon.navigator"] == "model:recon.navigator"
    # statements field retired
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
