"""Deterministic movement for one already-bound Android collection."""

from __future__ import annotations

from typing import Any


def move_collection(session: Any, table: dict, family: str) -> bool:
    """Scroll only the region described by the current collection candidate."""
    traversal = table.get("traversal")
    bounds = table.get("_region_bounds")
    client = getattr(session, "client", None)
    viewport = getattr(client, "viewport_size", None)
    if (
        not isinstance(traversal, dict)
        or traversal.get("type") != "scroll"
        or family not in {"scroll_forward", "scroll_backward"}
        or not isinstance(bounds, list)
        or len(bounds) != 4
        or not all(isinstance(value, (int, float)) for value in bounds)
        or client is None
        or not viewport
    ):
        return False
    width, height = viewport
    x = (float(bounds[0]) + float(bounds[2])) * width / 2000
    y = (float(bounds[1]) + float(bounds[3])) * height / 2000
    direction = "down" if family == "scroll_forward" else "up"
    # Keep part of the prior cell window visible so lossless stitching has an
    # exact anchor. Collection traversal favors continuity over fling speed.
    result = client.scroll(direction, amount=2, x=x, y=y)
    return str(result).startswith("OK ")


__all__ = ["move_collection"]
