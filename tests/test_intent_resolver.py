"""Deterministic tests for the Intent Resolver (block rendering + normalization + empty-goal)."""

import gui_agent.core.orchestrator.intent_resolver as ir
from gui_agent.core.orchestrator.intent_resolver import (
    EntityRef,
    IntentResolution,
    intent_block,
    resolve_intent,
)


def test_intent_block_renders_ladder_for_approximate_and_single_for_exact():
    res = IntentResolution(entities=[
        EntityRef(mention="Olivia zip jacket", type="product", match_mode="approximate", search_key="Olivia"),
        EntityRef(mention="WO-2024-007", type="order", match_mode="exact", search_key="WO-2024-007"),
    ])
    blk = intent_block(res)
    assert blk is not None and blk.id == "runtime.intent_resolution"
    assert blk.budget == "required" and blk.priority == 16
    # approximate → exact-then-fuzzy ladder, naming the column-by-type intent
    assert "先用精确值「Olivia zip jacket」" in blk.content and "关键词「Olivia」" in blk.content
    assert "非 0 条" in blk.content and "Product 列" in blk.content
    # exact → single precise lookup, no fuzzy fallback
    assert "精确标识 → 直接用「WO-2024-007」" in blk.content


def test_intent_block_none_when_empty():
    assert intent_block(None) is None
    assert intent_block(IntentResolution(entities=[])) is None


def test_resolve_intent_empty_goal_skips_llm():
    # empty goal must not call the LLM at all
    assert resolve_intent("   ").entities == []


def test_resolve_intent_normalizes_mode_and_search_key(monkeypatch):
    # a model may return a bad match_mode / empty search_key — resolve_intent must normalize
    def _fake(_llm, _msgs, _schema, **_kw):
        return IntentResolution(entities=[
            EntityRef(mention="Olivia zip jacket", type="product", match_mode="FUZZY", search_key=""),
            EntityRef(mention="WO-7", type="order", match_mode="exact", search_key="WO-7"),
        ])
    monkeypatch.setattr(ir, "invoke_structured", _fake)
    monkeypatch.setattr(ir, "_llm", lambda: object())
    res = resolve_intent("find Olivia zip jacket")
    e0 = res.entities[0]
    assert e0.match_mode == "approximate"      # unknown mode → approximate
    assert e0.search_key == "Olivia zip jacket"  # empty key → defaults to the mention
    assert res.entities[1].match_mode == "exact"  # valid mode preserved
