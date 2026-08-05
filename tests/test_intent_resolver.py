"""Deterministic tests for semantic task supplementation."""

import gui_agent.core.router.intent as ir
from gui_agent.core.router import IntentResolution, resolve_intent


def test_empty_goal_skips_rewriter() -> None:
    assert resolve_intent("   ") == IntentResolution(semantic_supplement="")


def test_router_returns_one_trimmed_semantic_supplement(monkeypatch) -> None:
    def _fake(_llm, messages, schema, **_kwargs):
        assert schema is IntentResolution
        assert "Original task:\nambiguous task" in str(messages[-1].content)
        return IntentResolution(semantic_supplement="  missing relationship  ")

    monkeypatch.setattr(ir, "invoke_structured", _fake)

    assert resolve_intent(" ambiguous task ", llm=object()).model_dump() == {
        "semantic_supplement": "missing relationship",
    }


def test_empty_supplement_stays_empty(monkeypatch) -> None:
    monkeypatch.setattr(
        ir,
        "invoke_structured",
        lambda *_args, **_kwargs: IntentResolution(semantic_supplement=""),
    )

    assert resolve_intent("  preserve this task  ", llm=object()).semantic_supplement == ""


def test_resolution_schema_has_no_execution_or_entity_fields() -> None:
    assert set(IntentResolution.model_fields) == {"semantic_supplement"}
