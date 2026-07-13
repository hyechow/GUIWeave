"""State for traversing one bound view surface.

Pagination and scrolling are different adapter movements over the same core operation:
consume the current window, move within the same surface, and stop at a goal or boundary.
This module deliberately knows nothing about tables, controls, screenshots, or UI actions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


TraversalAction = Literal[
    "move_forward",
    "move_backward",
    "wait",
    "done",
    "exhausted",
    "ambiguous",
]
TraversalCoverage = Literal["complete", "from_current"]
TraversalTerminal = Literal["done", "exhausted"]


@dataclass(frozen=True)
class TraversalWindow:
    """One observation of a specific scrollable or paginated surface."""

    surface_id: str
    position_key: str
    content_key: str
    position: float | None = None
    at_start: bool | None = None
    at_end: bool | None = None
    can_forward: bool = False
    can_backward: bool = False


@dataclass(frozen=True)
class TraversalDecision:
    action: TraversalAction
    reason: str


def window_from_signal(
    signal: dict | None,
    *,
    surface_id: str,
    content_key: str,
) -> TraversalWindow | None:
    """Translate an adapter traversal signal without assigning it to another surface."""

    if not isinstance(signal, dict) or not surface_id:
        return None
    kind = signal.get("type")
    if kind == "paged":
        raw_index = signal.get("page_index")
        try:
            index = float(raw_index) if raw_index is not None else None
        except (TypeError, ValueError):
            index = None
        has_next = signal.get("has_next_page")
        has_prev = signal.get("has_prev_page")
        return TraversalWindow(
            surface_id=surface_id,
            position_key=f"page:{raw_index}" if raw_index is not None else "page:unknown",
            content_key=content_key,
            position=index,
            at_start=(index <= 1) if index is not None else (False if has_prev is True else None),
            at_end=(has_next is False),
            can_forward=(has_next is True),
            can_backward=(has_prev is True),
        )
    if kind == "scroll":
        raw_top = signal.get("scroll_top")
        try:
            top = float(raw_top) if raw_top is not None else None
        except (TypeError, ValueError):
            top = None
        can_more = signal.get("can_scroll_more")
        can_back = signal.get("can_scroll_back")
        at_start = signal.get("at_scroll_start")
        if at_start is None and top is not None:
            at_start = top <= 0
        return TraversalWindow(
            surface_id=surface_id,
            position_key=f"scroll:{raw_top}" if raw_top is not None else "scroll:unknown",
            content_key=content_key,
            position=top,
            at_start=bool(at_start) if at_start is not None else None,
            at_end=bool(signal.get("at_scroll_end")) if signal.get("at_scroll_end") is not None else None,
            can_forward=(can_more is True),
            can_backward=(can_back is True),
        )
    return None


class TraversalSession:
    """Own movement history for one logical traversal operation.

    Consumers decide what constitutes success. Adapters decide how a forward/backward
    movement is executed. The session only verifies that observations remain on the same
    surface and that a requested movement produced directional content progress.
    """

    def __init__(
        self,
        scope: str,
        *,
        coverage: TraversalCoverage = "from_current",
        boundary_status: TraversalTerminal = "done",
        no_progress_status: TraversalTerminal = "exhausted",
        max_moves: int = 20,
        no_progress_limit: int = 2,
    ) -> None:
        self.scope = scope
        self.coverage = coverage
        self.boundary_status = boundary_status
        self.no_progress_status = no_progress_status
        self.max_moves = max(1, max_moves)
        self.no_progress_limit = max(1, no_progress_limit)
        self.surface_id = ""
        self.moves = 0
        self.visited: set[tuple[str, str]] = set()
        self._last_window: TraversalWindow | None = None
        self._pending_move: Literal["forward", "backward"] | None = None
        self._stagnant = 0
        self._forward_started = False
        self._start_seen = False
        self._terminal: TraversalDecision | None = None

    def reset(self) -> None:
        self.surface_id = ""
        self.moves = 0
        self.visited.clear()
        self._last_window = None
        self._pending_move = None
        self._stagnant = 0
        self._forward_started = False
        self._start_seen = False
        self._terminal = None

    def observe(
        self,
        window: TraversalWindow,
        *,
        goal_satisfied: bool = False,
    ) -> TraversalDecision:
        if self._terminal is not None:
            return self._terminal
        if not self.surface_id:
            self.surface_id = window.surface_id
        elif window.surface_id != self.surface_id:
            return TraversalDecision(
                "ambiguous",
                f"traversal surface changed from {self.surface_id!r} to {window.surface_id!r}",
            )

        if window.at_start is True:
            self._start_seen = True

        if goal_satisfied and self._coverage_origin_reached(window):
            self._remember(window)
            return self._finish("done", "consumer goal is satisfied")

        progress = self._movement_progress(window)
        if progress is False:
            self._stagnant += 1
            if self._stagnant >= self.no_progress_limit:
                self._pending_move = None
                return self._finish(
                    self.no_progress_status,
                    "requested movement did not advance the bound surface",
                )
            return TraversalDecision("wait", "movement dispatched; bound surface has not advanced yet")
        if progress is True:
            self._stagnant = 0
            self._pending_move = None

        # Trust a bound adapter boundary after any pending movement has been acknowledged. This
        # prevents an asynchronously updated pager from completing while it still exposes the
        # prior window's content.
        if window.at_end is True:
            self._remember(window)
            return self._finish(self.boundary_status, "bound surface reached its forward boundary")

        self._remember(window)
        if self.moves >= self.max_moves:
            return self._finish(
                "exhausted",
                f"traversal exhausted its {self.max_moves}-move budget",
            )

        # Complete collection may rewind only before any forward movement. Once a forward edge
        # has been requested, a later page must never be mistaken for an initial mid-list frame.
        if (
            self.coverage == "complete"
            and not self._start_seen
            and not self._forward_started
            and window.at_start is False
            and window.can_backward
        ):
            return self._move("backward", "complete coverage starts by rewinding to the first window")

        if window.can_forward:
            return self._move("forward", "bound surface exposes a forward window")
        if self.coverage == "from_current" and window.can_backward:
            return self._move("backward", "target lies before the current window")
        return TraversalDecision("ambiguous", "bound surface exposes no reliable movement or boundary")

    def _coverage_origin_reached(self, window: TraversalWindow) -> bool:
        if self.coverage != "complete":
            return True
        return self._start_seen or window.at_start is not False or self._forward_started

    def _movement_progress(self, window: TraversalWindow) -> bool | None:
        if self._pending_move is None or self._last_window is None:
            return None
        previous = self._last_window
        if previous.position is not None and window.position is not None:
            if self._pending_move == "forward":
                directional = window.position > previous.position
            else:
                directional = window.position < previous.position
            if not directional:
                return False
            if previous.content_key and window.content_key:
                return window.content_key != previous.content_key
            return True
        if previous.content_key and window.content_key:
            return window.content_key != previous.content_key
        if (
            previous.position_key
            and window.position_key
            and "unknown" not in previous.position_key
            and "unknown" not in window.position_key
        ):
            return window.position_key != previous.position_key
        return None

    def _move(self, direction: Literal["forward", "backward"], reason: str) -> TraversalDecision:
        self.moves += 1
        self._pending_move = direction
        if direction == "forward":
            self._forward_started = True
            return TraversalDecision("move_forward", reason)
        return TraversalDecision("move_backward", reason)

    def _remember(self, window: TraversalWindow) -> None:
        self.visited.add((window.position_key, window.content_key))
        self._last_window = window

    def _finish(self, action: TraversalTerminal, reason: str) -> TraversalDecision:
        self._pending_move = None
        self._terminal = TraversalDecision(action, reason)
        return self._terminal


__all__ = [
    "TraversalAction",
    "TraversalDecision",
    "TraversalSession",
    "TraversalWindow",
    "window_from_signal",
]
