"""Replay task-185's exact Quantity filter from 20260728_092732."""

from __future__ import annotations

import json
from pathlib import Path

from gui_agent.adapters.browser.filter_state import typed_applied_filter_state
from gui_agent.core.filter_contract import (
    compile_filter_predicates,
    match_filter_state,
)

_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "replay/fixtures/browser/092732_quantity_filter/observation_turn_7.json"
)


def test_equal_bound_numeric_chip_matches_scalar_contract() -> None:
    payload = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    adapter_state = payload["adapter_state"]
    actual = typed_applied_filter_state(
        adapter_state["filters"],
        adapter_state["meta"],
    )
    expected = compile_filter_predicates(payload["contract_filters"])

    result = match_filter_state(expected, actual)

    assert actual.predicates["quantity"].operator == "eq"
    assert actual.predicates["quantity"].values == ["3"]
    assert result.status == "met"

