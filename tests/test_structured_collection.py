from __future__ import annotations

import pytest

from gui_agent.core.run.structured_collection import CellStream, materialize_cell_records


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


def test_materializer_uses_model_only_for_refs_and_preserves_source_text() -> None:
    cells = [
        _cell("banner", "banner", "Posts"),
        _cell("author", "a1", "Pupper", "now · @pupper"),
        _cell("body", "b1", "A dog’s day — don’t normalize me"),
        _cell("actions", "k1", "Reply", "Favorite"),
        _cell("author", "a2", "Pupper", "now · @pupper"),
        _cell("body", "b2", "A dog’s day — don’t normalize me"),
    ]

    def project(request, schema):
        if request["mode"] == "record_anchor":
            return schema.model_validate({"anchor_cell": "c1"})
        sources = [
            source
            for cell in request["record_cells"]
            for source in cell["sources"]
        ]
        by_value = {source["value"]: source["source_ref"] for source in sources}
        return schema.model_validate({
            "fields": [
                {"field": "author_handle", "source_ref": by_value["@pupper"]},
                {
                    "field": "content",
                    "source_ref": by_value["A dog’s day — don’t normalize me"],
                },
            ],
        })

    assert materialize_cell_records(
        cells,
        ["author_handle", "content"],
        project=project,
    ) == [{
        "author_handle": "@pupper",
        "content": "A dog’s day — don’t normalize me",
    }]


def test_materializer_rejects_generated_source_refs() -> None:
    cells = [_cell("row", "r1", "Alpha")]

    def project(request, schema):
        if request["mode"] == "record_anchor":
            return schema.model_validate({"anchor_cell": "c0"})
        return schema.model_validate({
            "fields": [{"field": "name", "source_ref": "invented"}],
        })

    with pytest.raises(ValueError, match="unknown source refs"):
        materialize_cell_records(cells, ["name"], project=project)
