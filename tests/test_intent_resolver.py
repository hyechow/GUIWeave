"""Deterministic tests for intent fact normalization."""

import gui_agent.core.router.intent as ir
from gui_agent.core.router import (
    EntityRef,
    IntentResolution,
    resolve_intent,
)


def test_collection_scope_has_coverage_but_no_retrieval_semantics():
    scope = EntityRef(
        mention="all existing variants",
        role="collection_scope",
        match_mode="approximate",
        search_key="variant",
        selector="all existing variants",
    )
    assert scope.cardinality == "set"
    assert scope.match_mode == "exact"
    assert scope.search_key == ""


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
