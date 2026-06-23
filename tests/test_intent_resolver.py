"""Deterministic tests for the Intent Resolver (intent_block rendering + normalization + empty-goal)."""

import gui_agent.core.router.intent as ir
from gui_agent.core.router import (
    EntityRef,
    IntentResolution,
    intent_block,
    resolve_intent,
)


def test_intent_block_renders_facts_only_for_approximate_and_exact():
    res = IntentResolution(entities=[
        EntityRef(mention="Olivia zip jacket", type="product", match_mode="approximate", search_key="Olivia"),
        EntityRef(mention="WO-2024-007", type="order", match_mode="exact", search_key="WO-2024-007"),
    ])
    blk = intent_block(res)
    assert blk is not None and blk.id == "runtime.intent_resolution"
    assert blk.priority == 21  # right after task_goal_block(20)
    # the DECISION (fuzzy allowed? which key?) is router-authoritative content — facts only,
    # no orchestration/strategy prose (that belongs to decomposer.md rule 4b)
    assert "允许模糊匹配" in blk.content and "关键词：Olivia" in blk.content
    assert "精确匹配" in blk.content  # exact entity rendered too, just without a key
    assert "若0条" not in blk.content and "阶梯" not in blk.content  # no strategy leakage


def test_intent_block_none_when_no_entities():
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
