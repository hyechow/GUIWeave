"""Deterministic json_repair salvage for malformed LLM structured output.

The highest-frequency structured-output crash: a task whose text carries literal double
quotes (e.g. WebArena 544 — "Update the description ... to \"{count} customer(s) love
it!\" ... or \"don't miss out...\"") that the model escapes imperfectly, producing invalid
JSON. Before this salvage, json.loads rejected it outright → invoke_structured raised →
decompose crashed → the whole run exited with a traceback. _parse_structured_response now
falls back to json_repair on any strict-parse failure, turning a hard crash into a
best-effort recovery. A hopeless payload still raises (no silent empty object).
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from gui_agent.core.supervisor.statement.schemas import _StatementTransitionResult
from llm.structured import _parse_structured_response, _repair_json_object


class _Plan(BaseModel):
    goal: str
    steps: list[str] = []


def test_unescaped_inner_double_quotes_are_repaired():
    # The 544 shape: the goal value contains literal double-quoted phrases the model failed
    # to escape, so the inner `"` prematurely closes the JSON string.
    broken = (
        '{\n'
        '  "goal": "Update description to "3 customer(s) love it!" when count>0",\n'
        '  "steps": ["go to reviews", "count 4-star", "write short description"]\n'
        '}'
    )
    with pytest.raises(ValueError):
        # Baseline: strict json.loads (inside the strict path) cannot handle it — proven by
        # _repair_json_object being the thing that recovers below.
        import json
        json.loads(broken)

    plan = _parse_structured_response(broken, _Plan)
    assert "customer(s) love it" in plan.goal
    assert plan.steps == ["go to reviews", "count 4-star", "write short description"]


def test_trailing_comma_is_repaired():
    broken = '{"goal": "x", "steps": ["a", "b",],}'
    plan = _parse_structured_response(broken, _Plan)
    assert plan.goal == "x"
    assert plan.steps == ["a", "b"]


def test_valid_json_takes_strict_path_unchanged():
    good = '{"goal": "reach page", "steps": ["nav"]}'
    plan = _parse_structured_response(good, _Plan)
    assert plan.goal == "reach page"
    assert plan.steps == ["nav"]


def test_code_fenced_malformed_json_is_repaired():
    broken = (
        '```json\n'
        '{"goal": "set to "done"", "steps": []}\n'
        '```'
    )
    plan = _parse_structured_response(broken, _Plan)
    assert "done" in plan.goal


def test_truncated_transition_reason_keeps_action_emitted_first():
    broken = (
        '{"kind":"act",'
        '"action":{"instruction":"Input Diana Tights",'
        '"atomic_role":"write","action_family":"input",'
        '"target_control":"frontend_label","target_value":"Diana Tights"},'
        '"reason":"DOM list shows `name="'
    )

    decision = _parse_structured_response(broken, _StatementTransitionResult)

    assert decision.kind == "act"
    assert decision.action is not None
    assert decision.action.target_control == "frontend_label"
    assert decision.action.target_value == "Diana Tights"


def test_repair_is_not_reported_successful_when_schema_is_incomplete(capsys):
    broken = '{"kind":"act","reason":"DOM list shows `name="'

    with pytest.raises(ValidationError, match="act transition requires one action"):
        _parse_structured_response(broken, _StatementTransitionResult)

    assert "恢复并通过 schema 校验" not in capsys.readouterr().out


def test_hopeless_payload_still_raises_not_silently_empty():
    # No JSON object at all → repair returns None → the original parse error surfaces,
    # rather than validating an empty {} into a defaults-only object.
    with pytest.raises(ValueError):
        _parse_structured_response("I could not complete this task, sorry.", _Plan)


def test_repair_helper_rejects_empty_and_nonobject():
    assert _repair_json_object("no braces here") is None
    assert _repair_json_object("[1, 2, 3]") is None  # a list is not a schema object
    assert _repair_json_object("{}") is None  # empty object must not pass as a recovery
