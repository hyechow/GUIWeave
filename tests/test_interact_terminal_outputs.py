"""Phase 3: Interact terminal outputs split by declared coverage.

``complete``/``best_effort`` ``list[record]`` returns materialize from the Journal-projected
CollectionView (including the independently persisted terminal slice); ``current_view`` and
scalar returns still come from the terminal frame. Deterministic, no LLM.
"""

from __future__ import annotations

from gui_agent.core.orchestrator.program import Interact, OutputSpec
from gui_agent.core.orchestrator.runner import StatementInvocation
from gui_agent.core.run.interactive import project_interact_outputs
from gui_agent.core.schemas import CollectionProvenance, CollectionSliceEvent, Observation


INSTANCE = "i1:collect"


def _collection_turn(index: int, records, record_ids, *, known_total=None, boundary="unknown", content_key=""):
    del record_ids
    return CollectionSliceEvent(
        event_ref=f"collection:{index}",
        after_turn=max(0, index - 1),
        statement_instance_id=INSTANCE,
        statement_id="collect",
        frame_ref=f"frame:{index}",
        collection_key="grid",
        provenance=CollectionProvenance(
            surface_fingerprint="table:grid",
            schema_fingerprint="schema",
            route="/grid",
        ),
        records=records,
        known_total=known_total,
        boundary=boundary,
        content_key=content_key,
        source="table",
    )


def _invocation(returns):
    return StatementInvocation(
        statement=Interact(
            id="collect",
            bind="rows",
            goal="collect rows",
            success="all rows observed",
            returns=returns,
        )
    )


def _observation(**updates) -> Observation:
    return Observation.model_validate({
        "png_bytes": b"\x89PNG\r\n\x1a\n",
        "source": "browser",
        "title": "Grid",
        **updates,
    })


def test_list_record_complete_return_comes_from_collection_view_not_terminal_frame(monkeypatch):
    # Two prior frames already collected all 5 records and reached the boundary.
    history = [
        _collection_turn(
            1,
            [{"id": "1"}, {"id": "2"}, {"id": "3"}],
            ["id:1", "id:2", "id:3"],
            known_total=5,
            boundary="has_next_page",
            content_key="k1",
        ),
        _collection_turn(
            2,
            [{"id": "4"}, {"id": "5"}],
            ["id:4", "id:5"],
            known_total=5,
            boundary="at_end",
            content_key="k2",
        ),
    ]
    invocation = _invocation({"rows": OutputSpec(type="list[record]", coverage="complete")})
    # Safety net: the terminal-frame vision reader must NOT be consulted for collection outputs.
    from gui_agent.core.orchestrator.primitives import structured_read as sr

    def _fail(*_a, **_k):
        raise AssertionError("structured_read must not run for complete-coverage collection outputs")

    monkeypatch.setattr(sr, "structured_read", _fail)

    outputs = project_interact_outputs(
        invocation, _observation(), history=history, instance_id=INSTANCE,
    )
    assert [row["id"] for row in outputs["rows"]] == ["1", "2", "3", "4", "5"]


def test_current_view_return_still_uses_terminal_frame():
    invocation = _invocation({"title": OutputSpec(type="text")})  # coverage defaults current_view
    outputs = project_interact_outputs(
        invocation,
        _observation(form_controls=[{"label": "Title", "value": "Hello"}]),
        history=[],
        instance_id=INSTANCE,
    )
    assert outputs["title"] == "Hello"


def test_mixed_return_splits_between_collection_and_terminal_sources():
    history = [
        _collection_turn(1, [{"id": "1"}, {"id": "2"}], ["id:1", "id:2"], known_total=2, boundary="at_end"),
    ]
    invocation = _invocation({
        "rows": OutputSpec(type="list[record]", coverage="complete"),
        "title": OutputSpec(type="text"),
    })
    outputs = project_interact_outputs(
        invocation,
        _observation(form_controls=[{"label": "Title", "value": "Summary"}]),
        history=history,
        instance_id=INSTANCE,
    )
    assert [row["id"] for row in outputs["rows"]] == ["1", "2"]
    assert outputs["title"] == "Summary"


def test_terminal_slice_is_materialized_from_journal_without_live_fold_in():
    # The loop appends the completing observation as an independent slice before Transition.
    # Materialization therefore needs no live observation fallback and is replay-identical.
    history = [
        _collection_turn(1, [{"ID": "1"}], ["row:1"], known_total=2, boundary="has_next_page", content_key="k1"),
        _collection_turn(2, [{"ID": "2"}], ["row:1"], known_total=2, boundary="at_end", content_key="k2"),
    ]
    invocation = _invocation({"rows": OutputSpec(type="list[record]", coverage="complete")})
    outputs = project_interact_outputs(
        invocation, _observation(), history=history, instance_id=INSTANCE,
    )
    # Page 1 contributed ID:1; the live terminal page (distinct slice) contributes ID:2.
    assert [row["ID"] for row in outputs["rows"]] == ["1", "2"]


def test_best_effort_coverage_also_materializes_from_view():
    history = [
        _collection_turn(1, [{"id": "1"}, {"id": "2"}], ["id:1", "id:2"], boundary="unknown"),
    ]
    invocation = _invocation({"rows": OutputSpec(type="list[record]", coverage="best_effort")})
    outputs = project_interact_outputs(
        invocation, _observation(), history=history, instance_id=INSTANCE,
    )
    assert [row["id"] for row in outputs["rows"]] == ["1", "2"]
