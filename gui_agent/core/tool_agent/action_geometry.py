"""Mechanical geometry helpers shared by dispatch and receipt classification."""

from __future__ import annotations

from typing import Any

from gui_agent.core.tool_agent.contracts import MaterializedFrame


def control_at_point(
    args: dict[str, Any], frame: MaterializedFrame,
) -> dict[str, Any] | None:
    """Return the smallest visible enhanced control containing the action point."""
    x, y = args.get("x"), args.get("y")
    if not all(isinstance(value, (int, float)) for value in (x, y)):
        return None
    matches: list[tuple[float, dict[str, Any]]] = []
    for control in frame.controls:
        rect = control.get("rect")
        if (
            control.get("in_viewport") is False
            or not isinstance(rect, dict)
            or not all(isinstance(rect.get(key), (int, float)) for key in ("x", "y"))
        ):
            continue
        cx, cy = float(rect["x"]), float(rect["y"])
        width, height = float(rect.get("w") or 0), float(rect.get("h") or 0)
        if abs(float(x) - cx) <= width / 2 and abs(float(y) - cy) <= height / 2:
            matches.append((max(1.0, width * height), control))
    return min(matches, key=lambda item: item[0])[1] if matches else None
