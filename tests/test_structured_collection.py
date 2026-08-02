from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from gui_agent.core.run.structured_collection import CellStream, materialize_cell_records
from gui_agent.core.run.structured_collection import _source_catalog


def _cell(kind: str, content: str, *texts: str) -> dict:
    return {
        "structural_key": kind,
        "content_key": content,
        "texts": list(texts),
        "controls": [],
    }


def test_cell_stream_joins_arbitrary_exact_overlap() -> None:
    stream = CellStream()
    stream.add([
        _cell("author", "a1", "@one"),
        _cell("body", "b1", "First"),
        _cell("actions", "k1", "Reply"),
    ])
    stream.add([
        _cell("body", "b1", "First"),
        _cell("actions", "k1", "Reply"),
        _cell("author", "a2", "@two"),
        _cell("body", "b2", "Second"),
    ])

    assert [cell["content_key"] for cell in stream.cells] == [
        "a1", "b1", "k1", "a2", "b2",
    ]


def test_cell_stream_uses_forward_order_to_disambiguate_repeated_cells() -> None:
    stream = CellStream()
    stream.add([
        _cell("media", "m1", "Media"),
        _cell("actions", "actions", "Favorite"),
        _cell("author", "author", "@pupper"),
        _cell("body", "body1", "First post"),
        _cell("media", "m2", "Media"),
    ])
    stream.add([
        _cell("media", "clipped"),
        _cell("author", "author", "@pupper"),
        _cell("body", "body1", "First post"),
        _cell("media", "m3", "Media"),
        _cell("actions", "actions", "Favorite"),
        _cell("author", "author", "@pupper"),
        _cell("body", "body2", "Second post"),
    ])

    assert [cell["content_key"] for cell in stream.cells][-3:] == [
        "actions", "author", "body2",
    ]


def test_mastodon_replay_keeps_exact_cells_at_larger_scroll_stride() -> None:
    replay = json.loads(
        (Path(__file__).parents[1] / "evals/android/acquire/mastodon_20260801.json")
        .read_text(encoding="utf-8")
    )

    def stitched(frames):
        stream = CellStream()
        previous = None
        for frame in frames:
            if frame["content_key"] != previous:
                stream.add(frame["cells"])
                previous = frame["content_key"]
        return [cell["content_key"] for cell in stream.cells]

    for frames in replay["collections"].values():
        assert stitched(frames[::2]) == stitched(frames)


def test_cell_stream_rejects_unprovable_or_conflicting_alignment() -> None:
    stream = CellStream()
    stream.add([_cell("row", "one", "One")])
    with pytest.raises(ValueError, match="overlap"):
        stream.add([_cell("row", "two", "Two")])

    stream = CellStream()
    stream.add([_cell("row", "anchor", "Anchor"), _cell("row", "old", "One")])
    with pytest.raises(ValueError, match="conflict"):
        stream.add([
            _cell("row", "anchor", "Anchor"),
            _cell("row", "changed", "Different"),
        ])


def test_cell_stream_ignores_stale_prefix_before_proven_forward_overlap() -> None:
    stream = CellStream()
    stream.add([
        _cell("row", "oct", "October"),
        _cell("row", "sep", "September"),
        _cell("row", "aug-a", "August A"),
        _cell("row", "aug-b", "August B"),
        _cell("row", "jul", "July"),
        _cell("row", "jun", "June"),
    ])

    stream.add([
        _cell("row", "aug-a", "August A"),
        _cell("row", "stale-aug-b", "August B clipped"),
        _cell("row", "aug-a", "August A"),
        _cell("row", "aug-b", "August B"),
        _cell("row", "jul", "July"),
        _cell("row", "jun", "June"),
        _cell("row", "may", "May"),
    ])

    assert [cell["content_key"] for cell in stream.cells] == [
        "oct", "sep", "aug-a", "aug-b", "jul", "jun", "may",
    ]


def test_cell_stream_replaces_conflicting_clipped_fragment_with_complete_cell() -> None:
    clipped = _cell("row", "partial", "item label")
    clipped["clipped_bottom"] = True
    complete = _cell("row", "complete", "item metadata")
    stream = CellStream()
    stream.add([
        _cell("row", "anchor", "Anchor"),
        clipped,
        _cell("row", "tail", "Tail"),
    ])

    stream.add([
        _cell("row", "anchor", "Anchor"),
        complete,
        _cell("row", "tail", "Tail"),
        _cell("row", "next", "Next"),
    ])

    assert [cell["content_key"] for cell in stream.cells] == [
        "anchor", "complete", "tail", "next",
    ]


def test_cell_stream_bridges_one_transient_incomplete_window() -> None:
    stream = CellStream()
    stream.add([_cell("row", key, key) for key in "abcdefghij"])
    stream.add([_cell("row", key, key) for key in "cdefghijkl"])

    # A settling sensor exposes only a small middle fragment.  The subsequent
    # forward window no longer overlaps this fragment, but does overlap the exact
    # accumulated stream and therefore still has one lossless placement.
    stream.add([_cell("row", key, key) for key in "fgh"])
    stream.add([_cell("row", key, key) for key in "ijklmnop"])

    assert [cell["content_key"] for cell in stream.cells] == list("abcdefghijklmnop")


def test_materializer_uses_model_only_for_refs_and_preserves_source_text() -> None:
    cells = [
        _cell("banner", "banner", "Posts"),
        _cell("author", "a1", "Pupper", "now · @pupper"),
        _cell("body", "b1", "A dog’s day — don’t normalize me"),
        _cell("actions", "k1", "Reply", "Favorite"),
        _cell("author", "a2", "Pupper", "now · @pupper"),
        _cell("body", "b2", "A dog’s day — don’t normalize me"),
    ]

    calls = []

    def project(request, schema):
        calls.append(request["mode"])
        if request["mode"] == "record_anchor":
            assert {item["anchor_cell"] for item in request["anchor_candidates"]} >= {
                "c1", "c2",
            }
            return schema.model_validate({"anchor_cell": "c1"})
        records = []
        assert request["requested_field_types"] == {
            "author_handle": "text",
            "content": "text",
        }
        for record in request["records"]:
            assert all(
                re.fullmatch(rf"{record['record']}\.s\d+", source["source_ref"])
                for source in record["sources"]
            )
            assert set(record["field_candidates"]) == {"author_handle", "content"}
            by_value = {
                source["value"]: source["source_ref"]
                for source in record["sources"]
            }
            records.append({
                "record": record["record"],
                "fields": [
                    {"field": "author_handle", "source_ref": by_value["@pupper"]},
                    {
                        "field": "content",
                        "source_ref": by_value["A dog’s day — don’t normalize me"],
                    },
                ],
            })
        return schema.model_validate({"records": records})

    assert materialize_cell_records(
        cells,
        ["author_handle", "content"],
        project=project,
        field_types={"author_handle": "text", "content": "text"},
    ) == [{
        "author_handle": "@pupper",
        "content": "A dog’s day — don’t normalize me",
    }]
    assert calls == ["record_anchor", "field_sources"]


def test_materializer_rejects_generated_source_refs() -> None:
    cells = [_cell("row", "r1", "Alpha")]

    def project(request, schema):
        if request["mode"] == "record_anchor":
            return schema.model_validate({"anchor_cell": "c0"})
        return schema.model_validate({
            "records": [{
                "record": "r0",
                "fields": [{"field": "name", "source_ref": "invented"}],
            }],
        })

    with pytest.raises(ValueError, match="source"):
        materialize_cell_records(cells, ["name"], project=project)


def test_materializer_accepts_empty_collection_projection() -> None:
    cells = [_cell("row", "r1", "Alpha")]

    def project(_request, schema):
        return schema.model_validate({"anchor_cell": ""})

    assert materialize_cell_records(cells, ["name"], project=project) == []


def test_source_catalog_offers_opaque_exact_substring_spans() -> None:
    catalog, values = _source_catalog(
        [_cell("row", "r1", "Jul 11, 1.37 kB, ZIP archive")],
        "r0.",
        ["date"],
        {"date": "datetime"},
    )

    refs = [source["source_ref"] for source in catalog["sources"]]
    assert all(re.fullmatch(r"r0\.s\d+", ref) for ref in refs)
    assert "Jul 11" in values.values()
    assert "ZIP archive" in values.values()
    assert catalog["field_candidates"]["date"] == [
        ref for ref, value in values.items() if value == "Jul 11"
    ]


def test_field_projection_hides_incompatible_refs_but_keeps_context() -> None:
    cells = [
        _cell("total", "total-1", "Items 1", "Total: ￥ 367 .00"),
        _cell("total", "total-2", "Items 1", "Total: ￥ 829 .00"),
    ]

    def project(request, schema):
        if request["mode"] == "record_anchor":
            return schema.model_validate({"anchor_cell": "c0"})
        records = []
        for record in request["records"]:
            candidates = set(record["field_candidates"]["amount"])
            offered = {source["source_ref"] for source in record["sources"]}
            assert offered == candidates
            by_value = {
                source["value"]: source["source_ref"]
                for source in record["sources"]
            }
            value = next(
                value for value in by_value
                if str(value).startswith("￥") and ".00" in str(value)
            )
            records.append({
                "record": record["record"],
                "fields": [{"field": "amount", "source_ref": by_value[value]}],
            })
        return schema.model_validate({"records": records})

    assert materialize_cell_records(
        cells,
        ["amount"],
        project=project,
        field_types={"amount": "money"},
    ) == [
        {"amount": "￥ 367 .00"},
        {"amount": "￥ 829 .00"},
    ]
