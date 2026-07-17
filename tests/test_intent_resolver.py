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


def test_intent_block_renders_multi_value_members_as_separate_atoms():
    block = intent_block(IntentResolution(entities=[
        EntityRef(
            mention="blue and purple",
            role="qualifier_value",
            value_members=["blue", "purple"],
            match_mode="exact",
        ),
        EntityRef(
            mention="XXS",
            role="target_value",
            match_mode="exact",
        ),
    ]))

    assert block is not None
    assert "原子值=['blue', 'purple']" in block.content
    assert "同一选择组" in block.content
    assert "不得额外创建/改写其定义" in block.content
    assert "可作为任务写入目标、必要时建立定义前置：['XXS']" in block.content
    assert "只允许在最终 mutation 中选择、禁止建立独立定义阶段：['blue', 'purple']" in block.content


def test_collection_scope_has_coverage_but_no_retrieval_semantics():
    scope = EntityRef(
        mention="all existing variants",
        role="collection_scope",
        match_mode="approximate",
        search_key="variant",
        selector="all existing variants",
    )
    block = intent_block(IntentResolution(entities=[scope]))

    assert scope.cardinality == "set"
    assert scope.match_mode == "exact"
    assert scope.search_key == ""
    assert block is not None
    assert "成员覆盖范围「all existing variants」" in block.content
    assert "禁止对该短语做 exact→fallback 检索" in block.content
    assert "covers_set" in block.content


def test_legacy_value_introduction_pair_normalizes_at_model_boundary():
    qualifier = EntityRef.model_validate({
        "mention": "blue",
        "role": "value",
        "introduction": "not_required",
    })
    target = EntityRef.model_validate({
        "mention": "XXS",
        "role": "value",
        "introduction": "required",
    })

    assert qualifier.role == "qualifier_value"
    assert target.role == "target_value"
    assert "introduction" not in qualifier.model_dump()


def test_collection_scope_accepts_null_irrelevant_wire_fields():
    scope = EntityRef.model_validate({
        "mention": "all existing variants",
        "role": "collection_scope",
        "type": None,
        "match_mode": None,
        "search_key": None,
        "selector": None,
    })

    assert scope.type == "generic"
    assert scope.match_mode == "exact"
    assert scope.search_key == ""
    assert scope.selector == "all existing variants"


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
