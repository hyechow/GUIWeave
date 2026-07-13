from gui_agent.core.runtime.traversal import (
    TraversalSession,
    TraversalWindow,
    window_from_signal,
)


def _page(index: int, count: int, content: str, *, surface: str = "grid:products") -> TraversalWindow:
    return TraversalWindow(
        surface_id=surface,
        position_key=f"page:{index}",
        content_key=content,
        position=float(index),
        at_start=index == 1,
        at_end=index == count,
        can_forward=index < count,
        can_backward=index > 1,
    )


def test_complete_pagination_consumes_each_window_once() -> None:
    session = TraversalSession("rows", coverage="complete")

    assert session.observe(_page(1, 2, "rows-1")).action == "move_forward"
    assert session.observe(_page(2, 2, "rows-2")).action == "done"
    assert session.moves == 1


def test_complete_pagination_rewinds_only_when_initial_frame_is_mid_list() -> None:
    session = TraversalSession("rows", coverage="complete")

    assert session.observe(_page(2, 3, "rows-2")).action == "move_backward"
    assert session.observe(_page(1, 3, "rows-1")).action == "move_forward"
    assert session.observe(_page(2, 3, "rows-2")).action == "move_forward"
    assert session.observe(_page(3, 3, "rows-3")).action == "done"


def test_unknown_first_position_followed_by_page_two_does_not_rewind() -> None:
    """Regression for 213931: page 1 was consumed before its pager signal became precise.

    Once the session requested a forward move, page 2 is not a new mid-list starting point.
    """
    session = TraversalSession("rows", coverage="complete")
    first = TraversalWindow(
        surface_id="grid:products",
        position_key="page:unknown",
        content_key="first-20",
        at_start=None,
        at_end=False,
        can_forward=True,
    )

    assert session.observe(first).action == "move_forward"
    assert session.observe(_page(2, 2, "remaining-12")).action == "done"


def test_directional_progress_rejects_forward_move_that_returns_to_prior_page() -> None:
    session = TraversalSession("rows", coverage="complete", no_progress_limit=1)
    assert session.observe(_page(1, 3, "rows-1")).action == "move_forward"

    decision = session.observe(_page(1, 3, "rows-1"))

    assert decision.action == "exhausted"


def test_page_number_change_waits_for_async_content_update() -> None:
    session = TraversalSession("rows", coverage="complete", no_progress_limit=2)
    assert session.observe(_page(1, 2, "rows-1")).action == "move_forward"

    stale = session.observe(_page(2, 2, "rows-1"))
    fresh = session.observe(_page(2, 2, "rows-2"))

    assert stale.action == "wait"
    assert fresh.action == "done"


def test_collection_treats_repeated_scroll_frame_as_boundary_when_configured() -> None:
    session = TraversalSession(
        "feed",
        coverage="complete",
        no_progress_status="done",
        no_progress_limit=1,
    )
    window = TraversalWindow(
        surface_id="feed:main",
        position_key="scroll:unknown",
        content_key="frame-a",
        at_end=False,
        can_forward=True,
    )

    assert session.observe(window).action == "move_forward"
    assert session.observe(window).action == "done"


def test_target_traversal_exhausts_when_scroll_does_not_advance() -> None:
    session = TraversalSession(
        "target",
        coverage="from_current",
        boundary_status="exhausted",
        no_progress_limit=2,
    )
    window = TraversalWindow(
        surface_id="document:editor",
        position_key="target-y:1200",
        content_key="target:configurations",
        position=-1200,
        can_forward=True,
    )

    assert session.observe(window).action == "move_forward"
    assert session.observe(window).action == "wait"
    assert session.observe(window).action == "exhausted"


def test_surface_change_is_ambiguous_instead_of_consuming_foreign_evidence() -> None:
    session = TraversalSession("rows", coverage="complete")
    assert session.observe(_page(1, 2, "rows-1", surface="grid:a")).action == "move_forward"

    decision = session.observe(_page(2, 2, "rows-2", surface="grid:b"))

    assert decision.action == "ambiguous"


def test_window_from_signal_preserves_pager_and_scroll_geometry() -> None:
    paged = window_from_signal(
        {
            "type": "paged",
            "page_index": 2,
            "has_next_page": False,
            "has_prev_page": True,
        },
        surface_id="grid:x",
        content_key="rows",
    )
    scrolled = window_from_signal(
        {
            "type": "scroll",
            "scroll_top": 640,
            "can_scroll_more": True,
            "can_scroll_back": True,
            "at_scroll_end": False,
        },
        surface_id="document:x",
        content_key="frame",
    )

    assert paged is not None and paged.position == 2 and paged.can_backward
    assert scrolled is not None and scrolled.position == 640 and scrolled.can_forward
