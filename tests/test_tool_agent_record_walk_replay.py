"""Replay the task-116 killed run's frames through the record-walk mechanism.

Validates the deterministic linked-detail walk design against real recorded
data: traversal controls must classify cleanly on every editor/grid frame, and
editor observations replayed through the production crediting path must
reproduce the live run's resolution sequence exactly (including idempotent
re-visits).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gui_agent.adapters.browser.form_reader import traversal_action_of
from gui_agent.core.tool_agent.contracts import DataRequirement, MaterializedFrame
from gui_agent.core.tool_agent.perception import PerceptionMaterializer
from gui_agent.core.tool_agent.record_walk import (
    RecordWalkState,
    record_walk_step,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "tool_agent" / "task116_review_walk.json"
_EDITOR_TURNS = ("10", "11", "14", "16", "34", "37", "39")
_GRID_TURNS = ("13", "26")
# Full recorded order: editor visits interleaved with grid frames. Grid frames
# reset the driver's engagement exactly as they do in a live worker loop.
_FRAME_ORDER = ("10", "11", "13", "14", "15", "16", "26", "34", "35", "37", "38", "39")


@pytest.fixture(scope="module")
def recording() -> dict:
    return json.loads(_FIXTURE.read_text())


def test_editor_frames_classify_exactly_one_record_next(recording: dict) -> None:
    for turn in _EDITOR_TURNS:
        frame = recording["frames"][turn]
        marks = [
            action
            for action in (
                traversal_action_of(control) for control in frame["controls"]
            )
            if action
        ]
        assert marks.count("record_next") == 1, f"turn {turn}: {marks}"
        assert not any(mark.startswith("page_") for mark in marks), (
            f"turn {turn}: page traversal leaked onto editor: {marks}"
        )


def test_grid_frames_classify_page_next_without_record_leak(recording: dict) -> None:
    for turn in _GRID_TURNS:
        frame = recording["frames"][turn]
        marks = [
            action
            for action in (
                traversal_action_of(control) for control in frame["controls"]
            )
            if action
        ]
        assert "page_next" in marks, f"turn {turn}: {marks}"
        assert not any(mark.startswith("record_") for mark in marks), (
            f"turn {turn}: record traversal leaked onto grid: {marks}"
        )


def _requirement(recording: dict) -> DataRequirement:
    fields = ["nickname", *recording["detail_fields"]]
    return DataRequirement(
        id="tank_reviews",
        description="Tank reviews with linked-detail rating",
        row_schema={
            "type": "object",
            "properties": {field: {"type": "string"} for field in fields},
        },
        field_sources=recording["field_sources"],
    )


def _assembler() -> PerceptionMaterializer:
    materializer = object.__new__(PerceptionMaterializer)
    materializer._detail_collections = {}
    return materializer


def _credit_editor(
    materializer: PerceptionMaterializer,
    requirement: DataRequirement,
    recording: dict,
    turn: str,
    *,
    seed: bool = False,
) -> tuple[object, list, dict]:
    """Credit one recorded editor frame through the production assembly path."""
    result = materializer._assemble_detail_collection(
        state_key=("test", requirement.id),
        requirement=requirement,
        candidate_rows=(
            [dict(row) for row in recording["candidate_rows"]] if seed else []
        ),
        detail_fields=set(recording["detail_fields"]) if seed else set(),
        controls=recording["frames"][turn]["controls"],
        structured_rows=[],
        scope_status="met",
        surface="reviews",
        location="Reviews",
        scope_key="reviews",
    )
    assert result is not None, f"turn {turn}: assembly returned no state"
    return result


def _resolved_ordinals(state: object) -> list[int]:
    return [
        index + 1
        for index, row in enumerate(state.rows)
        if all(row.get(field) not in (None, "") for field in state.detail_fields)
    ]


def test_editor_observations_replay_live_resolution_sequence(recording: dict) -> None:
    requirement = _requirement(recording)
    materializer = _assembler()
    state = None
    for turn in _EDITOR_TURNS:
        state, _, _ = _credit_editor(
            materializer, requirement, recording, turn, seed=state is None
        )
        resolved = _resolved_ordinals(state)
        assert resolved == recording["expected_resolved_after"][turn], (
            f"turn {turn}: resolved {resolved} != live run "
            f"{recording['expected_resolved_after'][turn]}"
        )


def test_revisit_is_idempotent_under_replay(recording: dict) -> None:
    requirement = _requirement(recording)
    materializer = _assembler()
    state = None
    for turn in ("10", "11", "14"):
        state, _, _ = _credit_editor(
            materializer, requirement, recording, turn, seed=state is None
        )
    snapshot = [dict(row) for row in state.rows]

    # Re-visiting already-resolved editors (turns 16/37/39 in the run) must
    # neither move nor duplicate credits.
    for turn in ("16", "37", "39"):
        state, _, _ = _credit_editor(materializer, requirement, recording, turn)
    assert state.rows == snapshot


def _materialize(turn: str, frame_dict: dict) -> MaterializedFrame:
    """Rebuild a MaterializedFrame as the new normalization would produce it."""
    controls = []
    for control in frame_dict["controls"]:
        enriched = dict(control)
        action = traversal_action_of(enriched)
        if action:
            enriched["traversal_action"] = action
        controls.append(enriched)
    return MaterializedFrame(
        frame_id=f"frame:{turn}",
        screenshot_path="",
        url=frame_dict.get("url") or "",
        title=frame_dict.get("title") or "",
        controls=controls,
        requirement_scopes=frame_dict.get("requirement_scopes") or {},
    )


def test_driver_engages_every_editor_and_yields_grids(recording: dict) -> None:
    walk = RecordWalkState()
    for turn in _FRAME_ORDER:
        frame = _materialize(turn, recording["frames"][turn])
        step = record_walk_step(frame, walk)
        if turn in _EDITOR_TURNS:
            assert step is not None, f"turn {turn}: driver failed to engage"
            assert step.control.get("traversal_action") == "record_next"
            assert step.control.get("form_action") is None
        else:
            assert step is None, (
                f"turn {turn}: driver must yield grid navigation to the Worker"
            )


def test_driver_crediting_chain_reproduces_live_sequence(recording: dict) -> None:
    """Chain driver step → production credit over the recorded frame order.

    Every engaged frame credits exactly as the live run did; the duplicate
    visits (16/37/39) stay idempotent under the driver, which would have
    advanced past them instead of looping. Interleaved grid frames reset the
    driver's engagement as they do in a live worker loop.
    """
    requirement = _requirement(recording)
    materializer = _assembler()
    collection = None
    walk = RecordWalkState()
    for turn in _FRAME_ORDER:
        frame = _materialize(turn, recording["frames"][turn])
        step = record_walk_step(frame, walk)
        if turn not in _EDITOR_TURNS:
            assert step is None, f"turn {turn}: driver must yield grid frames"
            continue
        assert step is not None, f"turn {turn}: driver failed to engage"
        collection, _, _ = _credit_editor(
            materializer, requirement, recording, turn, seed=collection is None
        )
        resolved = _resolved_ordinals(collection)
        assert resolved == recording["expected_resolved_after"][turn]
        assert walk.stalls == 0, f"turn {turn}: driver stalled on live data"


def test_editor_verdicts_separate_fresh_credits_from_dups(recording: dict) -> None:
    """The assembly's current_editor verdict marks first visits as fresh and
    repeat visits as already-complete — the signal that lets the driver refuse
    to re-walk resolved records (the live run's turn killer)."""
    requirement = _requirement(recording)
    materializer = _assembler()
    collection = None
    expected_verdicts = {
        "10": (False, True),   # Mervin: fresh credit
        "11": (False, True),   # Trey: fresh credit
        "14": (False, True),   # Merrie: fresh credit
        "16": (True, True),    # Merrie again: already complete
        "34": (False, True),   # Shaunte: fresh credit
        "37": (True, True),    # Mervin again: already complete
        "39": (True, True),    # Mervin again: already complete
    }
    for turn in _EDITOR_TURNS:
        collection, _, progress = _credit_editor(
            materializer, requirement, recording, turn, seed=collection is None
        )
        verdict = progress["current_editor"]
        assert verdict is not None, f"turn {turn}: editor identity unmatched"
        assert (verdict["pre_resolved"], verdict["resolved"]) == expected_verdicts[turn], (
            f"turn {turn}: {verdict}"
        )
