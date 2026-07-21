"""Deterministic tests for CollectionView — the pure cross-frame Journal projection.

No LLM, no live platform. These lock the design's acceptance invariants for the reducer:
it is a frozen projection (no control API), transport-level slice dedup only (no business-key
merge), boundary evidence, and rebuilds identically after Journal serialization.
"""

from __future__ import annotations

import inspect

import gui_agent.core.run.collection_view as collection_view_module
from gui_agent.core.run.collection_view import (
    CollectionSliceEvent,
    CollectionView,
    build_collection_view,
    coverage_status,
    project_collection_slice,
)
from gui_agent.core.schemas import (
    CollectionProvenance,
    Observation,
    OutputSpec,
    StatementContract,
)


_FORBIDDEN_CONTROL_SURFACES = {"advance", "next_action", "should_continue", "is_complete", "phase"}


def _contract() -> StatementContract:
    return StatementContract(
        id="collect",
        goal="collect all matching rows",
        success="all reachable rows observed",
        returns={"rows": OutputSpec(type="list[record]")},
    )


def _turn(index: int, instance_id: str, frame: CollectionSliceEvent) -> CollectionSliceEvent:
    return frame.model_copy(update={
        "event_ref": f"collection:{index}",
        "after_turn": index - 1,
        "statement_instance_id": instance_id,
    })


def _frame(
    records: list[dict],
    record_ids: list[str],
    *,
    known_total: int | None = None,
    boundary: str = "unknown",
    content_key: str = "",
    surface_id: str = "table:grid",
    window_key: str = "",
) -> CollectionSliceEvent:
    del record_ids
    return CollectionSliceEvent(
        event_ref="collection:0",
        statement_instance_id="i1:collect",
        statement_id="collect",
        frame_ref="frame:0",
        collection_key=surface_id,
        provenance=CollectionProvenance(
            surface_fingerprint=surface_id,
            schema_fingerprint="schema",
            route="/grid",
        ),
        window_key=window_key,
        records=records,
        known_total=known_total,
        boundary=boundary,
        content_key=content_key,
        source="table",
    )


# --- 1. purity: no control surface ------------------------------------------


def test_collection_view_is_a_pure_projection_and_exposes_no_control_methods():
    view = build_collection_view(
        instance_id="i1:collect",
        contract=_contract(),
        history=[_turn(1, "i1:collect", _frame([{"id": "1"}], ["id:1"]))],
    )
    assert isinstance(view, CollectionView)
    for name in _FORBIDDEN_CONTROL_SURFACES:
        assert not hasattr(view, name), f"CollectionView must not expose {name!r}"
    exported = set(collection_view_module.__all__)
    assert not (_FORBIDDEN_CONTROL_SURFACES & exported)
    # No method on the dataclass returns a "should continue" verdict.
    assert not any(
        name.startswith("should") or name == "advance"
        for name, member in inspect.getmembers(view)
        if callable(member)
    )


# --- 2. slice-level dedup (transport), no business-key dedup -----------------


def test_slice_level_dedup_skips_reobserved_frame_rows():
    # Two frames with the SAME content_key = one slice re-observed -> rows counted once.
    history = [
        _turn(1, "i1:collect", _frame([{"id": "1"}, {"id": "2"}], ["row:1", "row:2"], content_key="k1", window_key="page:1")),
        _turn(2, "i1:collect", _frame([{"id": "1"}, {"id": "2"}], ["row:1", "row:2"], content_key="k1", window_key="page:1")),
    ]
    view = build_collection_view(instance_id="i1:collect", contract=_contract(), history=history)
    assert [row["id"] for row in view.records] == ["1", "2"]
    assert len(view.observed_segments) == 1  # second (re-observed) slice not counted as a segment
    assert len(view.seen_slice_keys) == 1


def test_identical_content_without_window_key_is_preserved():
    history = [
        _turn(1, "i1:collect", _frame([{"id": "same"}], ["row:1"], content_key="k1")),
        _turn(2, "i1:collect", _frame([{"id": "same"}], ["row:1"], content_key="k1")),
    ]
    view = build_collection_view(instance_id="i1:collect", contract=_contract(), history=history)
    assert [row["id"] for row in view.records] == ["same", "same"]
    assert view.may_contain_duplicates is True


def test_reobserved_slice_still_updates_boundary_evidence():
    # Same content after a no-op scroll may newly report at_end. Rows must not double-count,
    # but the later boundary must become the coverage frontier (else complete is stranded).
    history = [
        _turn(
            1, "i1:collect",
            _frame(
                    [{"id": "1"}], ["row:1"], content_key="k1",
                    boundary="has_next_page", known_total=1, window_key="page:1",
            ),
        ),
        _turn(
            2, "i1:collect",
            _frame(
                    [{"id": "1"}], ["row:1"], content_key="k1",
                    boundary="at_end", known_total=1, window_key="page:1",
            ),
        ),
    ]
    view = build_collection_view(instance_id="i1:collect", contract=_contract(), history=history)
    assert [row["id"] for row in view.records] == ["1"]
    assert [e.kind for e in view.boundary_evidence] == ["has_next_page", "at_end"]
    assert coverage_status(view) == "complete"


def test_overlapping_frames_keep_duplicate_rows():
    # Two DIFFERENT slices whose rows overlap (scroll window): duplicates are kept. Business
    # dedup is intentionally NOT performed at the collection layer (design acceptance).
    history = [
        _turn(1, "i1:collect", _frame([{"id": "1"}, {"id": "2"}], ["row:1", "row:2"], content_key="k1")),
        _turn(2, "i1:collect", _frame([{"id": "2"}, {"id": "3"}], ["row:1", "row:2"], content_key="k2")),
    ]
    view = build_collection_view(instance_id="i1:collect", contract=_contract(), history=history)
    assert [row["id"] for row in view.records] == ["1", "2", "2", "3"]


def test_same_name_different_record_kept_separately():
    # P0 negative: rows sharing a business field (name) but different records MUST both be
    # retained. Old business-key dedup (id/sku/.../name) collapsed these.
    history = [
        _turn(
            1, "i1:collect",
            _frame([{"name": "Alice", "id": "1"}], ["row:1"], content_key="k1"),
        ),
        _turn(
            2, "i1:collect",
            _frame([{"name": "Alice", "id": "2"}], ["row:1"], content_key="k2"),
        ),
    ]
    view = build_collection_view(instance_id="i1:collect", contract=_contract(), history=history)
    assert [row["id"] for row in view.records] == ["1", "2"]
    assert [row["name"] for row in view.records] == ["Alice", "Alice"]


def test_different_collection_provenance_never_mixes_records():
    history = [
        _turn(1, "i1:collect", _frame([{"id": "old"}], ["row:1"], surface_id="table:dashboard")),
        _turn(2, "i1:collect", _frame([{"id": "target"}], ["row:1"], surface_id="table:target")),
    ]
    view = build_collection_view(instance_id="i1:collect", contract=_contract(), history=history)
    assert [row["id"] for row in view.records] == ["target"]
    assert view.collection_key == "table:target"
    assert view.collection_keys == ("table:dashboard", "table:target")
    assert view.provenance_drift is True


# --- 3. known_total + at_end -> complete ------------------------------------


def test_known_total_and_at_end_boundary_yields_complete_coverage():
    history = [
        _turn(
            1,
            "i1:collect",
            _frame(
                records=[{"id": str(n)} for n in range(5)],
                record_ids=[f"id:{n}" for n in range(5)],
                known_total=5,
                boundary="at_end",
            ),
        ),
    ]
    view = build_collection_view(instance_id="i1:collect", contract=_contract(), history=history)
    assert view.known_total == 5
    assert coverage_status(view) == "complete"


def test_at_end_with_fewer_records_than_known_total_is_incomplete():
    history = [
        _turn(
            1,
            "i1:collect",
            _frame(
                records=[{"id": str(n)} for n in range(18)],
                record_ids=[f"id:{n}" for n in range(18)],
                known_total=38,
                boundary="at_end",
                window_key="page:2",
            ),
        ),
    ]
    view = build_collection_view(instance_id="i1:collect", contract=_contract(), history=history)
    assert coverage_status(view) == "incomplete"


# --- 4. coverage classification ----------------------------------------------


def test_collected_more_than_known_total_marks_conflicting():
    # Record count contradicts the authoritative known total -> genuine conflict.
    history = [
        _turn(
            1,
            "i1:collect",
            _frame(
                records=[{"id": str(n)} for n in range(5)],
                record_ids=[f"id:{n}" for n in range(5)],
                known_total=3,
            ),
        ),
    ]
    view = build_collection_view(instance_id="i1:collect", contract=_contract(), history=history)
    assert coverage_status(view) == "conflicting"


def test_multipage_sequence_ending_at_end_is_complete_not_conflicting():
    # Page 1 had a next page; the last page reached at_end. That is normal traversal,
    # not contradictory evidence -> complete once the total is met.
    history = [
        _turn(
            1,
            "i1:collect",
            _frame([{"id": "1"}], ["id:1"], known_total=2, boundary="has_next_page", content_key="k1"),
        ),
        _turn(
            2,
            "i1:collect",
            _frame([{"id": "2"}], ["id:2"], known_total=2, boundary="at_end", content_key="k2"),
        ),
    ]
    view = build_collection_view(instance_id="i1:collect", contract=_contract(), history=history)
    assert coverage_status(view) == "complete"


def test_overlapping_windows_at_end_do_not_false_conflict_with_total():
    history = [
        _turn(1, "i1:collect", _frame(
            [{"id": "1"}, {"id": "2"}], ["row:1", "row:2"],
            known_total=3, boundary="has_next_page", content_key="k1",
        )),
        _turn(2, "i1:collect", _frame(
            [{"id": "2"}, {"id": "3"}], ["row:1", "row:2"],
            known_total=3, boundary="at_end", content_key="k2",
        )),
    ]
    view = build_collection_view(instance_id="i1:collect", contract=_contract(), history=history)
    assert len(view.records) == 4
    assert view.may_contain_duplicates is True
    assert coverage_status(view) == "complete"


def test_known_total_drift_is_conflicting():
    history = [
        _turn(1, "i1:collect", _frame(
            [{"id": "1"}], ["row:1"], known_total=2,
            boundary="has_next_page", content_key="k1", window_key="page:1",
        )),
        _turn(2, "i1:collect", _frame(
            [{"id": "2"}], ["row:1"], known_total=3,
            boundary="at_end", content_key="k2", window_key="page:2",
        )),
    ]
    view = build_collection_view(instance_id="i1:collect", contract=_contract(), history=history)
    assert view.total_drift is True
    assert coverage_status(view) == "conflicting"


def test_has_next_page_without_at_end_is_incomplete():
    history = [
        _turn(1, "i1:collect", _frame([{"id": "1"}], ["id:1"], boundary="has_next_page")),
    ]
    view = build_collection_view(instance_id="i1:collect", contract=_contract(), history=history)
    assert coverage_status(view) == "incomplete"


def test_no_signals_is_unknown():
    history = [
        _turn(1, "i1:collect", _frame([{"id": "1"}], ["id:1"], boundary="unknown")),
    ]
    view = build_collection_view(instance_id="i1:collect", contract=_contract(), history=history)
    assert coverage_status(view) == "unknown"


# --- 5. current observation is not a historical fact -------------------------


def test_collection_view_ignores_current_observation_argument():
    class _FakeObservation:
        tables = [{"headers": ["id"], "rows": [{"id": "live"}], "traversal": {"type": "paged"}}]

    history = [_turn(1, "i1:collect", _frame([{"id": "1"}], ["id:1"]))]
    view = build_collection_view(
        instance_id="i1:collect",
        contract=_contract(),
        history=history,
        current_observation=_FakeObservation(),  # type: ignore[arg-type]
    )
    assert [row["id"] for row in view.records] == ["1"]


# --- 6. replay rebuild identical to live projection --------------------------


def test_replay_rebuild_is_identical_to_live_projection():
    history = [
        _turn(1, "i1:collect", _frame([{"id": "1"}], ["id:1"], known_total=3, boundary="has_next_page", content_key="k1")),
        _turn(2, "i1:collect", _frame([{"id": "2"}, {"id": "3"}], ["id:2", "id:3"], known_total=3, boundary="at_end", content_key="k2")),
    ]
    live = build_collection_view(instance_id="i1:collect", contract=_contract(), history=history)

    serialized = f"[{','.join(turn.model_dump_json() for turn in history)}]"
    import json

    restored = [CollectionSliceEvent.model_validate(obj) for obj in json.loads(serialized)]
    replayed = build_collection_view(instance_id="i1:collect", contract=_contract(), history=restored)

    assert replayed.records == live.records
    assert replayed.seen_slice_keys == live.seen_slice_keys
    assert replayed.known_total == live.known_total
    assert [e.kind for e in replayed.boundary_evidence] == [e.kind for e in live.boundary_evidence]
    assert coverage_status(replayed) == "complete"
    assert replayed.last_move_result == "new_content"


# --- sensor: project_collection_slice ---------------------------------------


def test_project_collection_slice_returns_none_without_list_record_return():
    contract = StatementContract(
        id="read",
        goal="read one field",
        success="field read",
        returns={"title": OutputSpec(type="text")},
    )

    class _Obs:
        tables = [{"headers": ["id"], "rows": [{"id": "1"}]}]

    assert project_collection_slice(
        _Obs(), contract, instance_id="i1:read", after_turn=0,
        event_ref="collection:1", frame_ref="frame:1",
    ) is None  # type: ignore[arg-type]


def test_project_collection_slice_projects_rows_and_provenance():
    contract = _contract()

    class _Obs:
        tables = [
            {
                "headers": ["ID", "Name"],
                "rows": [{"ID": "1", "Name": "a"}, {"ID": "2", "Name": "b"}],
                "total_records": 2,
                "traversal": {"type": "paged", "has_next_page": False},
            }
        ]

    frame = project_collection_slice(
        _Obs(), contract, instance_id="i1:collect", after_turn=0,
        event_ref="collection:1", frame_ref="frame:1",
    )  # type: ignore[arg-type]
    assert frame is not None
    assert {row["ID"] for row in frame.records} == {"1", "2"}
    assert frame.collection_key
    assert frame.provenance.schema_fingerprint
    assert frame.known_total == 2
    assert frame.boundary == "at_end"
    assert frame.source == "table"
