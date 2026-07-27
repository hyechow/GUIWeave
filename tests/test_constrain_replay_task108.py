"""Replay the task-108 frame without reviving label-based semantics.

The retained observation contains a button labelled ``Apply Filters`` but no
structured query action. Its label must remain observation text, not become a
runtime permission or postcondition.
"""

from __future__ import annotations

import json
from pathlib import Path

from gui_agent.core.filter_contract import compile_filter_predicates
from gui_agent.core.schemas import (
    CollectionIntent,
    Observation,
    StatementContract,
)
from gui_agent.core.supervisor.statement.observation_view import build_observation_view

_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "replay/fixtures/browser/194421_constrain_filter/observation_turn_5.json"
)


def _recorded_observation() -> Observation:
    payload = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    return Observation.model_validate({**payload["observation"], "png_bytes": b"x"})


def _contract() -> StatementContract:
    return StatementContract(
        id="c1",
        goal="Narrow the Orders collection",
        success="The exact Status predicate is active",
        interaction_intent=CollectionIntent(
            phase="constrain",
            entity="Orders",
            predicates=compile_filter_predicates({"Status": "Complete"}),
        ),
    )


def test_recorded_label_does_not_fabricate_structured_query_action() -> None:
    observation = _recorded_observation()
    contract = _contract()
    view = build_observation_view(contract, observation, [])
    apply = next(
        item for item in view.affordances
        if str(item.get("label") or "").strip().casefold() == "apply filters"
    )
    assert "activate" in (apply.get("supported_operations") or [])
    assert apply.get("query_action") is None
