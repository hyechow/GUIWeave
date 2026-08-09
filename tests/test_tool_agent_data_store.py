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


def test_collection_rows_deduplicate_overlap_between_visual_windows() -> None:
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

    assert collection.row_count == 3
    assert store.collection_rows(collection.ref) == [
        {"label": "alpha", "metric": 3},
        {"label": "beta", "metric": 2},
        {"label": "gamma", "metric": 1},
    ]
