from __future__ import annotations

import pytest

from gui_agent.core.tool_agent.data_store import RuntimeDataStore


ROW_SCHEMA = {
    "type": "object",
    "properties": {
        "label": {"type": "string"},
        "metric": {"type": "integer"},
    },
    "required": ["label", "metric"],
    "additionalProperties": False,
}


def _put_window(
    store: RuntimeDataStore,
    frame_id: str,
    rows: list[dict],
    *,
    context: str,
    partial: bool,
    provider: str = "vision",
    **coverage,
):
    return store.put_chunk(
        requirement_id="records",
        frame_id=frame_id,
        provider=provider,
        rows=rows,
        row_schema=ROW_SCHEMA,
        coverage={
            "window_context": context,
            "partial": partial,
            "at_end": not partial,
            **coverage,
        },
    )


def test_data_store_exposes_refs_but_resolves_values_only_at_runtime() -> None:
    store = RuntimeDataStore()
    rows = [{"label": "alpha", "metric": 3}]

    chunk, collection, created = store.put_chunk(
        requirement_id="records",
        frame_id="frame:1",
        provider="vision",
        rows=rows,
        row_schema=ROW_SCHEMA,
        coverage={"end_visible": True},
    )

    assert created is True
    assert "alpha" not in chunk.model_dump_json()
    assert "alpha" not in collection.model_dump_json()
    assert store.collection_chunks(collection.ref) == [rows]
    assert store.collection_rows(collection.ref) == rows


def test_data_store_deduplicates_same_rows_across_observations() -> None:
    store = RuntimeDataStore()
    kwargs = dict(
        requirement_id="records",
        provider="structured",
        rows=[{"label": "alpha", "metric": 3}],
        row_schema=ROW_SCHEMA,
        coverage={"end_visible": True},
    )
    first, _, first_created = store.put_chunk(frame_id="frame:1", **kwargs)
    second, collection, second_created = store.put_chunk(frame_id="frame:2", **kwargs)

    assert first.ref == second.ref
    assert first_created is True
    assert second_created is False
    assert collection.chunk_refs == [first.ref]


def test_collection_rows_remove_overlap_between_visual_windows() -> None:
    store = RuntimeDataStore()
    _put_window(
        store,
        "frame:1",
        [{"label": "alpha", "metric": 3}, {"label": "beta", "metric": 2}],
        context="surface",
        partial=True,
    )
    _, collection, _ = _put_window(
        store,
        "frame:2",
        [{"label": "beta", "metric": 2}, {"label": "gamma", "metric": 1}],
        context="surface",
        partial=False,
    )

    assert collection.row_count == 3
    assert store.collection_rows(collection.ref) == [
        {"label": "alpha", "metric": 3},
        {"label": "beta", "metric": 2},
        {"label": "gamma", "metric": 1},
    ]
    assert collection.coverage["may_contain_duplicates"] is True


def test_visual_collection_backfills_rows_above_initial_bottom_window() -> None:
    store = RuntimeDataStore()
    rows = [{"label": "first", "metric": 1}, {"label": "second", "metric": 2}]
    _, bottom, _ = _put_window(
        store, "frame:1",
        rows=rows[1:],
        context="surface", partial=True, start_visible=False, end_visible=True,
    )

    assert store.mark_scroll_end(bottom.ref).coverage["status"] == "incomplete"

    _, backfilled, _ = _put_window(
        store, "frame:2",
        rows=rows,
        context="surface", partial=True, start_visible=True, end_visible=True,
    )

    assert store.collection_rows(backfilled.ref) == rows
    assert store.mark_scroll_end(backfilled.ref).coverage["status"] == "complete"
    structured = RuntimeDataStore()
    _, collection, _ = _put_window(
        structured, "frame:1", rows=rows[:1], context="surface", partial=True,
        provider="structured", source_scope="structured_surface", scope_status="met",
        start_visible=True, movement={"type": "scroll"},
    )
    assert structured.mark_scroll_end(collection.ref).coverage["status"] == "complete"


def test_static_unknown_total_stays_incomplete_with_a_clipped_start() -> None:
    store = RuntimeDataStore()
    _, collection, _ = _put_window(
        store, "frame:1", [{"label": "middle", "metric": 2}],
        context="surface", partial=False, provider="structured",
        source_scope="structured_surface", scope_status="met",
        start_visible=False, movement={"type": "static"},
        traversal_type="static",
    )

    assert collection.coverage["status"] == "incomplete"


@pytest.mark.parametrize(
    ("rows", "source_metadata", "expected"),
    [
        ([{"label": "only", "metric": 27}], {}, "complete"),
        (
            [{"label": "a", "metric": 27}, {"label": "b", "metric": 23}],
            {},
            "conflicting",
        ),
        ([{"label": "only", "metric": 27}], {"total_records": 0}, "conflicting"),
        ([{"label": "only", "metric": 27}], {"total_records": 2}, "conflicting"),
        (
            [{"label": "only", "metric": 27}],
            {"movement": {"has_next_page": True}},
            "conflicting",
        ),
    ],
)
def test_singleton_requires_exactly_one_authoritative_record(
    rows: list[dict], source_metadata: dict, expected: str,
) -> None:
    store = RuntimeDataStore()
    _, collection, _ = _put_window(
        store, "frame:1", rows, context="summary",
        partial=True,
        scope_status="met",
        cardinality="one",
        start_visible=True,
        **source_metadata,
    )

    assert collection.coverage["status"] == expected


@pytest.mark.parametrize(
    ("contexts", "expected_count"),
    [
        (("detail:1", "detail:2"), 2),
        (("surface", "surface"), 1),
    ],
    ids=("distinct-contexts", "advancing-window"),
)
def test_visual_collection_merges_identical_rows_by_window_context(
    contexts: tuple[str, str],
    expected_count: int,
) -> None:
    store = RuntimeDataStore()
    rows = [{"label": "same", "metric": 1}]
    _put_window(
        store, "frame:1", rows, context=contexts[0], partial=True
    )
    _, collection, _ = _put_window(
        store, "frame:2", rows, context=contexts[1], partial=False
    )

    assert collection.row_count == expected_count
    assert store.collection_rows(collection.ref) == rows * expected_count
    assert collection.coverage["status"] == "complete"


@pytest.mark.parametrize("provider", ["vision", "structured"])
def test_pager_requires_every_page_before_completion(provider: str) -> None:
    store = RuntimeDataStore()
    common = dict(
        requirement_id="records",
        provider=provider,
        row_schema=ROW_SCHEMA,
    )

    def put(page: int, page_count: int = 4, total_records: int | None = None):
        return store.put_chunk(
            frame_id=f"frame:{page}",
            rows=[{"label": f"page {page}", "metric": page}],
            coverage={
                "window_context": f"page:{page}",
                "source_scope": (
                    "structured_surface" if provider == "structured"
                    else "visual_viewport"
                ),
                "page_index": page,
                "page_count": page_count,
                "total_records": total_records,
                "has_next_page": page < 4,
                "at_end": page == 4,
                "partial": page < 4,
            },
            **common,
        )

    put(1, total_records=2)
    _, collection, _ = put(4, total_records=2)

    assert collection.coverage["status"] == "incomplete"
    assert collection.coverage["pages_seen"] == [1, 4]

    store = RuntimeDataStore()
    put(1)
    put(4)
    put(2)
    put(3)
    _, collection, created = put(4)

    assert created is False
    assert collection.row_count == 4
    assert collection.coverage["status"] == "complete"
    assert collection.coverage["pages_seen"] == [1, 2, 3, 4]

    _, collection, created = put(4, page_count=5)
    assert created is False
    assert collection.coverage["status"] == "conflicting"


def test_structured_collection_preserves_identical_business_rows_on_distinct_pages() -> None:
    store = RuntimeDataStore()
    common = dict(
        requirement_id="records",
        provider="structured",
        rows=[{"label": "same", "metric": 1}],
        row_schema=ROW_SCHEMA,
    )
    _, first_collection, _ = store.put_chunk(
        frame_id="frame:1",
        coverage={
            "source_scope": "structured_surface",
            "window_key": "page:1",
            "page_index": 1,
            "page_count": 2,
            "has_next_page": True,
            "at_end": False,
            "partial": True,
        },
        **common,
    )
    _, collection, _ = store.put_chunk(
        frame_id="frame:2",
        coverage={
            "source_scope": "structured_surface",
            "window_key": "page:2",
            "page_index": 2,
            "page_count": 2,
            "has_next_page": False,
            "at_end": True,
            "partial": False,
        },
        **common,
    )

    assert first_collection.coverage["status"] == "incomplete"
    assert collection.coverage["status"] == "complete"
    assert collection.row_count == 2
    assert store.collection_rows(collection.ref) == [
        {"label": "same", "metric": 1},
        {"label": "same", "metric": 1},
    ]


def test_structured_collection_deduplicates_repeated_page_snapshot() -> None:
    store = RuntimeDataStore()
    kwargs = dict(
        requirement_id="records",
        provider="structured",
        rows=[{"label": "same", "metric": 1}],
        row_schema=ROW_SCHEMA,
        coverage={
            "source_scope": "structured_surface",
            "window_key": "page:1",
            "page_index": 1,
            "page_count": 2,
            "has_next_page": True,
            "at_end": False,
            "partial": True,
        },
    )

    first, _, first_created = store.put_chunk(frame_id="frame:1", **kwargs)
    second, collection, second_created = store.put_chunk(frame_id="frame:2", **kwargs)

    assert first.ref == second.ref
    assert first_created is True
    assert second_created is False
    assert collection.row_count == 1
    assert collection.coverage["pages_seen"] == [1]


def test_collection_provenance_does_not_mix_rows_across_filter_scopes() -> None:
    store = RuntimeDataStore()
    common = dict(
        requirement_id="records",
        provider="structured",
        row_schema=ROW_SCHEMA,
    )
    _, first, _ = store.put_chunk(
        frame_id="frame:1",
        rows=[{"label": "before", "metric": 1}],
        coverage={
            "collection_key": "surface:unfiltered",
            "source_scope": "structured_surface",
            "scope_status": "met",
            "traversal_type": "static",
            "partial": False,
        },
        **common,
    )
    _, filtered, _ = store.put_chunk(
        frame_id="frame:2",
        rows=[{"label": "after", "metric": 2}],
        coverage={
            "collection_key": "surface:filtered",
            "source_scope": "structured_surface",
            "scope_status": "met",
            "requested_filters": {"Status": "Complete"},
            "applied_filters": {"Status": "Complete"},
            "traversal_type": "static",
            "partial": False,
        },
        **common,
    )

    assert store.collection_rows(first.ref) == [{"label": "after", "metric": 2}]
    assert filtered.coverage["collection_key"] == "surface:filtered"
    assert filtered.coverage["requested_filters"] == {"Status": "Complete"}
