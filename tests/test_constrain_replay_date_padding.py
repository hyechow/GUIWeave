"""Replay task-108's unpadded date-filter chip from 20260724_074335."""

from __future__ import annotations

import json
from pathlib import Path

from gui_agent.core.filter_contract import (
    AppliedFilterState,
    compare_filter_state,
    compile_filter_predicates,
)
from gui_agent.core.schemas import Observation

_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "replay/fixtures/browser/074335_date_filter_chip/observation_turn_9.json"
)

_CONTRACT_FILTERS = {
    "Status": "Complete",
    "Purchase Date": "01/01/2023 - 05/31/2023",
}


def _recorded_observation() -> Observation:
    payload = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    observation = payload["observation"]
    applied = observation.get("applied_filters") or {}
    return Observation.model_validate({
        **observation,
        "png_bytes": b"x",
        "applied_filter_state": AppliedFilterState(
            predicates=compile_filter_predicates(applied),
            coverage="complete",
            source="replay_fixture",
        ),
    })


def test_recorded_unpadded_range_matches_only_the_same_contract() -> None:
    observation = _recorded_observation()
    assert (
        observation.applied_filters["Purchase Date"]
        != _CONTRACT_FILTERS["Purchase Date"]
    )
    assert compile_filter_predicates(observation.applied_filters) == (
        compile_filter_predicates(_CONTRACT_FILTERS)
    )
    assert compare_filter_state(
        compile_filter_predicates(_CONTRACT_FILTERS),
        observation.applied_filter_state,
    ) is True
    assert compare_filter_state(
        compile_filter_predicates({
            "Status": "Complete",
            "Purchase Date": "01/01/2023 - 06/30/2023",
        }),
        observation.applied_filter_state,
    ) is False
