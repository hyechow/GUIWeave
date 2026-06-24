"""Stateful traversal controller for list_read collection.

This controller maintains traversal session state (starting page, visited pages,
direction) and outputs deterministic action recommendations based on the current
traversal state from the sensor layer.

Designed to replace hardcoded traversal rules in planner prompts with
deterministic logic based on DOM-based traversal state detection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class TraversalController:
    """Stateful controller for table/grid traversal during list_read collection.

    Maintains session state to avoid oscillation bugs (e.g., 1↔2 ping-pong)
    and outputs deterministic action recommendations based on the current
    traversal state from the sensor layer.

    Args:
        var: The list_read variable name this controller is associated with.

    Attributes:
        var: The list_read variable name.
        started_at_page: The page number where collection started (None until first known page).
        seen_start_page: Whether we've seen page 1 (used to detect if we started mid-list).
        visited_pages: Set of page indices we've already visited (to avoid revisiting).
        direction: Current traversal direction ('forward', 'backward', 'none').
        _unknown_count: Consecutive frames with unknown traversal state (for fallback).
    """

    var: str
    started_at_page: int | None = None
    seen_start_page: bool = False
    visited_pages: set[int] = field(default_factory=set)
    direction: Literal["forward", "backward", "none"] = "none"
    _unknown_count: int = 0

    # Constants
    _UNKNOWN_THRESHOLD = 3  # Consecutive unknown frames before falling back to LLM

    def update(self, traversal: dict | None) -> str:
        """Receive current traversal state and return recommended action.

        Args:
            traversal: The traversal dict from Observation.tables, with keys:
                - type: 'paged', 'scroll', or 'unknown'
                - page_index: Current page number (for paged lists)
                - page_count: Total pages (for paged lists)
                - has_next_page: Whether next page exists (for paged lists)
                - has_prev_page: Whether prev page exists (for paged lists)
                - can_scroll_more: Whether more content can be loaded by scrolling (for scroll lists)
                - at_scroll_end: Whether we've reached the scroll end (for scroll lists)

        Returns:
            A recommendation string:
            - "paginate_next": Click next page button
            - "paginate_prev": Click previous page button
            - "scroll_down": Scroll down to load more
            - "stay": Keep current page (wait for LLM decision)
            - "done": Collection complete (no more traversal needed)

        Decision logic:
            1. First frame (traversal is None): Initialize, return "stay"
            2. type='unknown': Increment counter, return "stay" if below threshold (LLM fallback)
            3. type='paged':
               - Not on page 1 (page_index > 1) and started_at_page is None → "paginate_prev"
               - On page 1 → Record started_at_page=1, seen_start_page=True
               - Forward mode and has_next_page → "paginate_next"
               - !has_next_page → "done" (reached last page)
            4. type='scroll':
               - can_scroll_more → "scroll_down"
               - at_scroll_end → "done"
        """
        # Case 1: No traversal data (first frame or sensor failure)
        if traversal is None:
            self._unknown_count = 0
            return "stay"

        t_type = traversal.get("type")

        # Case 2: Unknown traversal type
        if t_type == "unknown" or t_type is None:
            self._unknown_count += 1
            if self._unknown_count >= self._UNKNOWN_THRESHOLD:
                # Fall back to LLM after too many unknowns
                return "stay"
            return "stay"

        # Reset unknown counter on valid traversal
        self._unknown_count = 0

        # Case 3: Paged list
        if t_type == "paged":
            return self._update_paged(traversal)

        # Case 4: Scroll list
        if t_type == "scroll":
            return self._update_scroll(traversal)

        # Fallback for any other type
        return "stay"

    def _update_paged(self, traversal: dict) -> str:
        """Update state for paged list and return recommendation."""
        page_index = traversal.get("page_index")
        has_next = traversal.get("has_next_page")
        has_prev = traversal.get("has_prev_page")

        # Record visit
        if page_index is not None:
            self.visited_pages.add(page_index)

        # First decision: if we don't know where we started, try to find page 1
        if self.started_at_page is None:
            if page_index == 1:
                # We're on page 1, this is our starting point
                self.started_at_page = 1
                self.seen_start_page = True
                self.direction = "forward"
                if has_next:
                    return "paginate_next"
                return "done"
            elif page_index is not None and page_index > 1 and has_prev:
                # We're mid-list, go back to page 1 first
                self.direction = "backward"
                return "paginate_prev"
            else:
                # Can't determine starting point, proceed forward
                self.started_at_page = page_index or 1
                if has_next:
                    return "paginate_next"
                return "done"

        # After starting point is established
        if self.direction == "backward":
            # We're going back to page 1
            if page_index == 1:
                self.direction = "forward"
                self.seen_start_page = True
                if has_next:
                    return "paginate_next"
                return "done"
            return "paginate_prev"

        # Forward mode
        if has_next:
            return "paginate_next"

        # No next page, we're done
        return "done"

    def _update_scroll(self, traversal: dict) -> str:
        """Update state for scroll list and return recommendation."""
        can_scroll = traversal.get("can_scroll_more", False)
        at_end = traversal.get("at_scroll_end", False)

        if at_end or not can_scroll:
            return "done"

        return "scroll_down"

    def reset(self, var: str | None = None) -> None:
        """Reset controller state for a new collection.

        Args:
            var: If provided, update the var name; otherwise keep current.
        """
        if var is not None:
            self.var = var
        self.started_at_page = None
        self.seen_start_page = False
        self.visited_pages.clear()
        self.direction = "none"
        self._unknown_count = 0
