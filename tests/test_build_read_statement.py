"""Lock the shared Read-statement constructor shape.

The production runtime read op and the replay tool both build the Read through
``build_read_statement`` so the statement can never drift between execution and
replay (a prior replay re-implementation missed task_goal and the field-type
descriptions, breaking derived reads). This test locks the contract.
"""

from __future__ import annotations

from gui_agent.core.run.contracts import build_read_statement


def test_returns_carry_field_type_descriptions() -> None:
    stmt = build_read_statement(
        ["body", "start_ts"],
        {"body": "text", "start_ts": "datetime"},
    )
    assert stmt.reads["body"].name == "body"
    assert stmt.reads["start_ts"].source == "field"
    assert "text" in stmt.returns["body"].description
    assert "datetime" in stmt.returns["start_ts"].description
    assert all(spec.type == "json" for spec in stmt.returns.values())


def test_unknown_types_fall_back_to_blank_description() -> None:
    stmt = build_read_statement(["sku"])
    assert stmt.returns["sku"].description == ""
