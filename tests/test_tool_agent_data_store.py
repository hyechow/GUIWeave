from __future__ import annotations

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


def test_collection_rows_preserve_overlap_between_visual_windows() -> None:
    store = RuntimeDataStore()
    common = dict(
        requirement_id="records",
        provider="vision",
        row_schema=ROW_SCHEMA,
        coverage={"end_visible": False},
    )
    store.put_chunk(
        frame_id="frame:1",
        rows=[{"label": "alpha", "metric": 3}, {"label": "beta", "metric": 2}],
        **common,
    )
    _, collection, _ = store.put_chunk(
        frame_id="frame:2",
        rows=[{"label": "beta", "metric": 2}, {"label": "gamma", "metric": 1}],
        **common,
    )

    assert collection.row_count == 4
    assert store.collection_rows(collection.ref) == [
        {"label": "alpha", "metric": 3},
        {"label": "beta", "metric": 2},
        {"label": "beta", "metric": 2},
        {"label": "gamma", "metric": 1},
    ]
    assert collection.coverage["may_contain_duplicates"] is True


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
