"""Tests for TraversalController stateful traversal logic."""

from gui_agent.core.orchestrator.traversal_controller import TraversalController


def test_controller_initial_state():
    """Initial state should have empty traversal history."""
    c = TraversalController(var="reviews")
    assert c.var == "reviews"
    assert c.started_at_page is None
    assert c.seen_start_page is False
    assert c.visited_pages == set()
    assert c.direction == "none"
    assert c._unknown_count == 0


def test_first_frame_no_traversal():
    """First frame with no traversal data should return 'stay'."""
    c = TraversalController(var="reviews")
    assert c.update(None) == "stay"


def test_unknown_traversal_type():
    """Unknown traversal type should increment counter and return 'stay'."""
    c = TraversalController(var="reviews")
    assert c.update({"type": "unknown"}) == "stay"
    assert c._unknown_count == 1
    assert c.update({"type": "unknown"}) == "stay"
    assert c._unknown_count == 2


def test_unknown_threshold_fallback():
    """After 3+ unknown frames, still return 'stay' (LLM fallback)."""
    c = TraversalController(var="reviews")
    c.update({"type": "unknown"})
    c.update({"type": "unknown"})
    c.update({"type": "unknown"})
    assert c._unknown_count == 3
    assert c.update({"type": "unknown"}) == "stay"  # At threshold


def test_paged_start_on_page_1():
    """Starting on page 1 should record started_at_page and go forward."""
    c = TraversalController(var="reviews")
    traversal = {
        "type": "paged",
        "page_index": 1,
        "page_count": 3,
        "has_next_page": True,
        "has_prev_page": False,
    }
    assert c.update(traversal) == "paginate_next"
    assert c.started_at_page == 1
    assert c.seen_start_page is True
    assert c.direction == "forward"
    assert 1 in c.visited_pages


def test_paged_start_on_page_2():
    """Starting mid-list (page 2) should go back to page 1 first."""
    c = TraversalController(var="reviews")
    traversal = {
        "type": "paged",
        "page_index": 2,
        "page_count": 3,
        "has_next_page": True,
        "has_prev_page": True,
    }
    assert c.update(traversal) == "paginate_prev"
    assert c.direction == "backward"
    assert 2 in c.visited_pages


def test_paged_mid_list_to_page_1():
    """Going back from page 2 to page 1, then forward."""
    c = TraversalController(var="reviews")
    # Start on page 2
    c.update({"type": "paged", "page_index": 2, "has_next_page": True, "has_prev_page": True})
    assert c.direction == "backward"

    # Reach page 1
    result = c.update({"type": "paged", "page_index": 1, "has_next_page": True, "has_prev_page": False})
    assert result == "paginate_next"
    assert c.direction == "forward"
    assert c.seen_start_page is True


def test_paged_forward_to_last_page():
    """Forward traversal should continue until last page."""
    c = TraversalController(var="reviews")
    # Page 1
    c.update({"type": "paged", "page_index": 1, "has_next_page": True, "has_prev_page": False})
    # Page 2
    c.update({"type": "paged", "page_index": 2, "has_next_page": True, "has_prev_page": True})
    # Page 3 (last)
    result = c.update({"type": "paged", "page_index": 3, "has_next_page": False, "has_prev_page": True})
    assert result == "done"


def test_paged_last_page_no_page_index():
    """When page_index is missing but has_next_page is False, we're done."""
    c = TraversalController(var="reviews")
    # Start on page 1
    c.update({"type": "paged", "page_index": 1, "has_next_page": True, "has_prev_page": False})
    # Last page (no page_index provided)
    result = c.update({"type": "paged", "page_index": None, "has_next_page": False, "has_prev_page": True})
    assert result == "done"


def test_paged_single_page():
    """Single-page list should return 'done' immediately."""
    c = TraversalController(var="reviews")
    traversal = {
        "type": "paged",
        "page_index": 1,
        "page_count": 1,
        "has_next_page": False,
        "has_prev_page": False,
    }
    assert c.update(traversal) == "done"


def test_scroll_can_scroll_more():
    """Scroll list with more content should recommend scroll_down."""
    c = TraversalController(var="feed")
    traversal = {
        "type": "scroll",
        "can_scroll_more": True,
        "at_scroll_end": False,
    }
    assert c.update(traversal) == "scroll_down"


def test_scroll_at_end():
    """Scroll list at end should return 'done'."""
    c = TraversalController(var="feed")
    traversal = {
        "type": "scroll",
        "can_scroll_more": False,
        "at_scroll_end": True,
    }
    assert c.update(traversal) == "done"


def test_scroll_at_end_only_flag():
    """When only at_scroll_end is provided, should return 'done'."""
    c = TraversalController(var="feed")
    traversal = {
        "type": "scroll",
        "at_scroll_end": True,
    }
    assert c.update(traversal) == "done"


def test_reset():
    """Reset should clear all state except var if not provided."""
    c = TraversalController(var="reviews")
    # Start on page 1 to set started_at_page
    c.update({"type": "paged", "page_index": 1, "has_next_page": True, "has_prev_page": False})
    assert c.started_at_page == 1
    assert len(c.visited_pages) > 0

    c.reset()
    assert c.started_at_page is None
    assert c.visited_pages == set()
    assert c.direction == "none"
    assert c.var == "reviews"  # var preserved


def test_reset_with_new_var():
    """Reset with new var should update var name."""
    c = TraversalController(var="reviews")
    c.reset(var="orders")
    assert c.var == "orders"


def test_unknown_count_resets_on_valid_traversal():
    """Unknown counter should reset when we get valid traversal."""
    c = TraversalController(var="reviews")
    c.update({"type": "unknown"})
    c.update({"type": "unknown"})
    assert c._unknown_count == 2

    # Valid traversal resets counter
    c.update({"type": "paged", "page_index": 1, "has_next_page": True, "has_prev_page": False})
    assert c._unknown_count == 0


def test_paged_first_page_with_next_scenario():
    """A paged grid on page 1 with a next page should go forward."""
    c = TraversalController(var="reviews")
    traversal = {
        "type": "paged",
        "page_index": 1,
        "page_count": 2,
        "has_next_page": True,
        "has_prev_page": False,
    }
    assert c.update(traversal) == "paginate_next"


def test_paged_last_page_scenario():
    """A paged grid on the last page should return 'done'."""
    c = TraversalController(var="reviews")
    # Start on page 1
    c.update({"type": "paged", "page_index": 1, "has_next_page": True, "has_prev_page": False})
    # Page 2
    result = c.update({"type": "paged", "page_index": 2, "has_next_page": False, "has_prev_page": True})
    assert result == "done"


def test_oscillation_prevention():
    """Once we've moved forward from page 1, don't go back unless we started mid-list."""
    c = TraversalController(var="reviews")
    # Start on page 1
    c.update({"type": "paged", "page_index": 1, "has_next_page": True, "has_prev_page": False})
    assert c.direction == "forward"

    # Page 2
    c.update({"type": "paged", "page_index": 2, "has_next_page": True, "has_prev_page": True})
    assert c.direction == "forward"

    # Page 3 (last)
    result = c.update({"type": "paged", "page_index": 3, "has_next_page": False, "has_prev_page": True})
    assert result == "done"  # Don't oscillate back to page 2
