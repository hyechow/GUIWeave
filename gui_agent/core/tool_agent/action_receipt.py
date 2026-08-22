"""Deterministic classification of executed action receipts."""

from __future__ import annotations

from typing import Any

from gui_agent.core.tool_agent.action_geometry import control_at_point
from gui_agent.core.tool_agent.contracts import MaterializedFrame


def is_confirmed_selection_commit(
    args: dict[str, Any], frame: MaterializedFrame,
) -> bool:
    """Recognize one adapter-declared selection commit; never admit or reject actions."""
    target = control_at_point(args, frame)
    candidates = [
        item for item in frame.controls
        if item.get("in_viewport") is not False
        and item.get("selection_mode") == "multiple"
    ]
    return bool(
        target
        and target.get("form_action") == "commit"
        and any(item.get("is_filter") is True for item in frame.controls)
        and candidates
        and all(bool(item.get("selected")) for item in candidates)
    )
