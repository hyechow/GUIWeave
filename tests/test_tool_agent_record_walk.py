"""Deterministic record-walk driver invariants.

The driver engages only on an unambiguous active walk (one scope, one visible
record_next control), never acts twice without frame progress, yields on
ambiguity, and caps its steps relative to the candidate count.
"""

from __future__ import annotations

from typing import Any

from gui_agent.core.tool_agent.contracts import MaterializedFrame
from gui_agent.core.tool_agent.record_walk import (
    MAX_WALK_STALLS,
    RecordWalkState,
    record_walk_step,
)


def _control(
    label: str = "Next",
    *,
    traversal: str | None = "record_next",
    in_viewport: bool = True,
    rect: dict[str, float] | None = None,
) -> dict[str, Any]:
    control: dict[str, Any] = {"kind": "button", "label": label}
    if traversal:
        control["traversal_action"] = traversal
    if not in_viewport:
        control["in_viewport"] = False
    control["rect"] = rect if rect is not None else {"x": 500.0, "y": 120.0, "w": 60, "h": 24}
    return control


def _frame(
    *,
    scopes: dict[str, Any] | None = None,
    controls: list[dict[str, Any]] | None = None,
    url: str = "http://example.test/edit/id/1/",
    surfaces: list[dict[str, Any]] | None = None,
) -> MaterializedFrame:
    return MaterializedFrame(
        frame_id="frame:test",
        screenshot_path="",
        url=url,
        title="Edit Record",
        controls=controls if controls is not None else [_control()],
        visible_collection_regions=[],
        structured_surfaces=surfaces or [],
        applied_filters={},
        requirement_scopes=scopes if scopes is not None else {
            "reviews": {
                "status": "met",
                "detail_resolution": {
                    "status": "active",
                    "candidate_records": 3,
                    "resolved_candidate_ordinals": [1],
                    "next_unresolved_candidate": {"ordinal": 2, "fields": {}},
                },
            }
        },
        chunks=[],
        collections=[],
        missing_requirements=[],
    )


def _active_scope(resolved: list[int], candidates: int = 3) -> dict[str, Any]:
    return {
        "reviews": {
            "status": "met",
            "detail_resolution": {
                "status": "active",
                "candidate_records": candidates,
                "resolved_candidate_ordinals": resolved,
                "next_unresolved_candidate": {"ordinal": max(resolved, default=0) + 1, "fields": {}},
            },
        }
    }


def test_engages_on_active_editor_and_taps_next() -> None:
    state = RecordWalkState()
    step = record_walk_step(_frame(), state)

    assert step is not None
    assert step.control["label"] == "Next"
    assert step.resolved == (1,)
    assert step.candidate_records == 3
    assert state.engaged is True


def test_yields_when_scope_resolution_not_active() -> None:
    state = RecordWalkState()
    scope = _active_scope([1])["reviews"]
    scope["detail_resolution"]["status"] = "resolved"

    assert record_walk_step(_frame(scopes={"reviews": scope}), state) is None
    assert state.engaged is False


def test_yields_on_scope_or_control_ambiguity() -> None:
    state = RecordWalkState()
    two_scopes = {
        **_active_scope([1]),
        "other": _active_scope([1])["reviews"],
    }
    assert record_walk_step(_frame(scopes=two_scopes), state) is None

    two_controls = [_control(), _control()]
    assert record_walk_step(_frame(controls=two_controls), state) is None

    offscreen = [_control(in_viewport=False)]
    assert record_walk_step(_frame(controls=offscreen), state) is None

    no_rect = [_control(rect={"w": 10, "h": 10})]
    assert record_walk_step(_frame(controls=no_rect), state) is None


def test_yields_without_traversal_marker_even_on_nav_button() -> None:
    state = RecordWalkState()
    controls = [_control(traversal=None)]
    assert record_walk_step(_frame(controls=controls), state) is None


def test_stalls_after_repeated_no_progress_and_yields() -> None:
    state = RecordWalkState()
    frame = _frame()

    assert record_walk_step(frame, state) is not None
    # Identical frame again: the previous tap made no observable progress.
    assert record_walk_step(frame, state) is not None
    assert state.stalls == 1
    assert record_walk_step(frame, state) is None
    assert state.stalls == MAX_WALK_STALLS


def test_progress_resets_stall_count() -> None:
    state = RecordWalkState()
    assert record_walk_step(_frame(url="http://example.test/edit/id/1/"), state) is not None
    assert record_walk_step(_frame(url="http://example.test/edit/id/1/"), state) is not None
    assert state.stalls == 1
    # A new record (URL + field values change) resets the progress-stall budget;
    # growth resets the no-credit streak at the same time.
    grown = _active_scope([1, 2])
    assert record_walk_step(_frame(scopes=grown, url="http://example.test/edit/id/2/"), state) is not None
    assert state.stalls == 0
    assert state.no_credit_streak == 0


def _no_growth_scope() -> dict[str, Any]:
    return _active_scope([1, 2], candidates=5)


def test_yields_after_two_consecutive_no_credit_steps() -> None:
    """Walking through resolved/non-creditable records must hand control back
    so the Worker can re-aim at the exact next_unresolved identity instead of
    re-walking a resolved prefix (live failure: 13 wasted steps, turn death)."""
    state = RecordWalkState()
    scopes = _no_growth_scope()
    # First engagement: current editor credited (resolved grew into [1,2]).
    assert record_walk_step(_frame(scopes=scopes, url="http://example.test/edit/id/2/"), state) is not None
    # Next record produces no new credit: streak 1, still engaged.
    assert record_walk_step(_frame(scopes=scopes, url="http://example.test/edit/id/3/"), state) is not None
    assert state.no_credit_streak == 1
    # Second consecutive no-credit record: yield to the Worker.
    assert record_walk_step(_frame(scopes=scopes, url="http://example.test/edit/id/4/"), state) is None
    assert state.no_credit_streak == 2


def test_credit_resets_no_credit_streak() -> None:
    state = RecordWalkState()
    assert record_walk_step(_frame(scopes=_active_scope([1]), url="http://example.test/edit/id/1/"), state) is not None
    assert record_walk_step(_frame(scopes=_no_growth_scope(), url="http://example.test/edit/id/2/"), state) is not None
    assert state.no_credit_streak == 0
    # One no-credit step, then growth again: streak must restart.
    flat = _active_scope([1, 2], candidates=5)
    assert record_walk_step(_frame(scopes=flat, url="http://example.test/edit/id/3/"), state) is not None
    assert state.no_credit_streak == 1
    grown = _active_scope([1, 2, 3], candidates=5)
    assert record_walk_step(_frame(scopes=grown, url="http://example.test/edit/id/4/"), state) is not None
    assert state.no_credit_streak == 0


def _grid_frame(
    *,
    candidates: int,
    total: int,
    page_next_visible: bool = True,
    page_previous_visible: bool = False,
) -> MaterializedFrame:
    scopes = {
        "reviews": {
            "status": "met",
            "detail_resolution": {
                "status": "active",
                "candidate_records": candidates,
                "resolved_candidate_ordinals": [1],
                "next_unresolved_candidate": {"ordinal": 2, "fields": {}},
            },
        }
    }
    controls = []
    if page_next_visible is not None:
        controls.append(
            _control("Next page", traversal="page_next", in_viewport=page_next_visible)
        )
    if page_previous_visible:
        controls.append(
            _control("Previous page", traversal="page_previous", in_viewport=True)
        )
    return _frame(
        scopes=scopes,
        controls=controls,
        url="http://example.test/grid/",
        surfaces=[{"total_records": total, "row_count": min(candidates, 20)}],
    )


def test_pages_candidate_grid_when_short_of_total() -> None:
    state = RecordWalkState()
    step = record_walk_step(_grid_frame(candidates=20, total=27), state)
    assert step is not None
    assert step.control["traversal_action"] == "page_next"
    assert "20/27" in step.reason


def test_grid_page_requires_candidate_growth() -> None:
    state = RecordWalkState()
    assert record_walk_step(_grid_frame(candidates=20, total=27), state) is not None
    # Same candidate count after the press (page failed to append): yield.
    assert record_walk_step(_grid_frame(candidates=20, total=27), state) is None
    # Growth re-arms paging while still short of the total.
    state2 = RecordWalkState()
    assert record_walk_step(_grid_frame(candidates=20, total=40), state2) is not None
    step = record_walk_step(_grid_frame(candidates=27, total=40), state2)
    assert step is not None and "27/40" in step.reason


def test_grid_page_stops_at_total_and_requires_visibility() -> None:
    state = RecordWalkState()
    assert record_walk_step(_grid_frame(candidates=27, total=27), state) is None
    assert record_walk_step(
        _grid_frame(candidates=20, total=27, page_next_visible=False), state,
    ) is None


def test_grid_pages_backward_when_only_previous_direction_exists() -> None:
    """On the last page with candidates short of the total, the driver must
    page backward toward unobserved windows (live failure: worker fake-
    completed a 7/27 first window on page 2 of 2)."""
    state = RecordWalkState()
    step = record_walk_step(
        _grid_frame(
            candidates=7,
            total=27,
            page_next_visible=False,
            page_previous_visible=True,
        ),
        state,
    )
    assert step is not None
    assert step.control["traversal_action"] == "page_previous"
    assert "7/27" in step.reason


def test_grid_paging_yields_when_both_directions_exist() -> None:
    state = RecordWalkState()
    step = record_walk_step(
        _grid_frame(
            candidates=7,
            total=27,
            page_next_visible=True,
            page_previous_visible=True,
        ),
        state,
    )
    assert step is None


def _candidate_grid_frame(
    *,
    row_id: str = "335",
    anchors: int = 1,
    anchor_visible: bool = True,
) -> MaterializedFrame:
    scopes = {
        "reviews": {
            "status": "met",
            "detail_resolution": {
                "status": "active",
                "candidate_records": 20,
                "resolved_candidate_ordinals": [1, 2],
                "next_unresolved_candidate": {
                    "ordinal": 3,
                    "fields": {"nickname": "Teofila", "review_id": row_id},
                },
            },
        }
    }
    controls = [
        {
            "kind": "checkbox_input",
            "label": "335 Apr 19, 2023, 12:15:20 PM",
            "group_id": "grid_table:0",
            "rect": {"x": 40.0, "y": 300.0, "w": 16, "h": 16},
        },
        {
            "kind": "checkbox_input",
            "label": "337 Apr 19, 2023, 12:15:20 PM",
            "group_id": "grid_table:1",
            "rect": {"x": 40.0, "y": 330.0, "w": 16, "h": 16},
        },
    ]
    for index in range(anchors):
        controls.append({
            "kind": "a",
            "label": "Edit",
            "group_id": "grid_table:0",
            "rect": {"x": 900.0, "y": 300.0 + index, "w": 40, "h": 18},
            **({} if anchor_visible else {"in_viewport": False}),
        })
    return _frame(
        scopes=scopes,
        controls=controls,
        url="http://example.test/grid/",
        surfaces=[{"total_records": 20, "row_count": 20}],
    )


def test_opens_exact_candidate_row_by_recorded_id() -> None:
    step = record_walk_step(_candidate_grid_frame(), RecordWalkState())
    assert step is not None
    assert step.control["kind"] == "a"
    assert step.control["group_id"] == "grid_table:0"
    assert "id 335" in step.reason


def test_candidate_open_yields_on_ambiguity() -> None:
    # Two action anchors in the matched row group: not deterministic.
    assert record_walk_step(
        _candidate_grid_frame(anchors=2), RecordWalkState(),
    ) is None
    # Anchor offscreen: the Worker scrolls first.
    assert record_walk_step(
        _candidate_grid_frame(anchor_visible=False), RecordWalkState(),
    ) is None
    # No matching row for the recorded id: fall through to paging/yield.
    assert record_walk_step(
        _candidate_grid_frame(row_id="999"), RecordWalkState(),
    ) is None


def test_candidate_open_does_not_repeat_a_dead_tap() -> None:
    state = RecordWalkState()
    frame = _candidate_grid_frame()
    assert record_walk_step(frame, state) is not None
    # Identical frame again: the open tap did not navigate — yield.
    assert record_walk_step(frame, state) is None


def _editor_frame_with_verdict(verdict: dict[str, Any] | None) -> MaterializedFrame:
    scopes = {
        "reviews": {
            "status": "met",
            "detail_resolution": {
                "status": "active",
                "candidate_records": 5,
                "resolved_candidate_ordinals": [1, 2],
                "next_unresolved_candidate": {"ordinal": 3, "fields": {}},
                "current_editor": verdict,
            },
        }
    }
    return _frame(scopes=scopes, url="http://example.test/edit/id/2/")


def test_current_editor_verdict_gates_engagement() -> None:
    # Freshly completed this frame: the only green light to advance.
    fresh = _editor_frame_with_verdict(
        {"ordinal": 2, "pre_resolved": False, "resolved": True}
    )
    step = record_walk_step(fresh, RecordWalkState())
    assert step is not None

    # Already complete before this frame: resolved prefix — yield.
    done = _editor_frame_with_verdict(
        {"ordinal": 1, "pre_resolved": True, "resolved": True}
    )
    assert record_walk_step(done, RecordWalkState()) is None

    # Gaps on the current record: the Worker finishes or investigates it.
    gapped = _editor_frame_with_verdict(
        {"ordinal": 3, "pre_resolved": False, "resolved": False}
    )
    assert record_walk_step(gapped, RecordWalkState()) is None

    # Identity unmatched (record outside the candidate set): yield.
    outsider = _editor_frame_with_verdict(None)
    assert record_walk_step(outsider, RecordWalkState()) is None


def test_step_cap_scales_with_candidate_count() -> None:
    state = RecordWalkState()
    steps = 0
    for index in range(12):
        # Resolution grows every frame so neither stall budget fires;
        # only the step cap can stop the walk.
        scopes = _active_scope(list(range(1, index + 2)), candidates=1)
        frame = _frame(
            scopes=scopes,
            url=f"http://example.test/edit/id/{100 + index}/",
        )
        if record_walk_step(frame, state) is None:
            break
        steps += 1
    # cap = max(10, 2*1 + 4) = 10
    assert steps == 10
    scopes = _active_scope(list(range(1, 15)), candidates=1)
    assert record_walk_step(
        _frame(scopes=scopes, url="http://example.test/edit/id/999/"),
        state,
    ) is None
