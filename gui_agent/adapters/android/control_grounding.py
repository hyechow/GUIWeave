"""Bound visual Android coordinates to current UIAutomator control geometry."""

from __future__ import annotations

import math
import re
from typing import Any

from gui_agent.core.schemas import BaseActionDecision


def _compatible(control: dict[str, Any], action_type: str) -> bool:
    kind = str(control.get("kind") or "").casefold()
    if action_type == "type":
        return any(token in kind for token in ("input", "textbox", "editor"))
    return action_type in {"tap", "click"} and kind in {
        "button", "switch", "select", "text_input",
    }


def _matches_described_type(description: str, control: dict[str, Any]) -> bool:
    words = set(re.findall(r"[a-z0-9]+", description.casefold()))
    kind = str(control.get("kind") or "").casefold()
    if words.intersection({"input", "field", "textbox", "editor"}):
        return any(token in kind for token in ("input", "textbox", "editor"))
    if "button" in words:
        return kind == "button"
    if words.intersection({"switch", "toggle"}):
        return kind == "switch"
    return True


def _contains_visible_name(description: str, label: str) -> bool:
    """Match a complete ASCII phrase or a normalized non-ASCII label."""
    if not description or not label:
        return False
    if label.isascii():
        label_tokens = re.findall(r"[a-z0-9]+", label.casefold())
        description_tokens = re.findall(r"[a-z0-9]+", description.casefold())
        width = len(label_tokens)
        return bool(
            label_tokens
            and sum(len(token) for token in label_tokens) >= 3
            and any(
                description_tokens[index:index + width] == label_tokens
                for index in range(len(description_tokens) - width + 1)
            )
        )
    normalized_label = "".join(
        character.casefold() for character in label if character.isalnum()
    )
    normalized_description = "".join(
        character.casefold() for character in description if character.isalnum()
    )
    return len(normalized_label) >= 2 and normalized_label in normalized_description


def ground_action_to_android_control(
    decision: BaseActionDecision,
    controls: list[dict[str, Any]] | None,
) -> BaseActionDecision:
    """Correct one bounded visual near-miss using current Android controls.

    A unique visible label named by the Worker wins over a neighboring row that
    happens to contain the estimated point.  Otherwise only a unique nearby
    geometry candidate is used. Missing, distant, or ambiguous evidence fails
    open to the original visual coordinate.
    """
    action = decision.action
    action_type = str(action.action_type or "")
    if (
        action_type not in {"tap", "click", "type"}
        or action.x is None
        or action.y is None
    ):
        return decision

    candidates: list[tuple[float, float, float, dict[str, Any], float, float]] = []
    semantic: list[tuple[float, dict[str, Any], float, float]] = []
    for control in controls or []:
        if (
            not isinstance(control, dict)
            or not _compatible(control, action_type)
            or control.get("in_viewport") is False
        ):
            continue
        rect = control.get("rect")
        if not isinstance(rect, dict) or not all(
            isinstance(rect.get(axis), (int, float))
            for axis in ("x", "y", "w", "h")
        ):
            continue
        center_x, center_y = float(rect["x"]), float(rect["y"])
        width, height = max(0.0, float(rect["w"])), max(0.0, float(rect["h"]))
        if (
            not (0 <= center_x < 1000 and 0 <= center_y < 1000)
            or (width >= 950 and height >= 600)
        ):
            continue
        center_distance = math.hypot(
            float(action.x) - center_x,
            float(action.y) - center_y,
        )
        edge_x = max(abs(float(action.x) - center_x) - width / 2, 0.0)
        edge_y = max(abs(float(action.y) - center_y) - height / 2, 0.0)
        edge_distance = math.hypot(edge_x, edge_y)
        area = max(1.0, width * height)
        label = str(control.get("label") or "").strip()
        described_type = _matches_described_type(
            str(action.description or ""), control,
        )
        if described_type and _contains_visible_name(
            str(action.description or ""), label,
        ):
            semantic.append((center_distance, control, center_x, center_y))
        max_center_distance = 220.0 if action_type == "type" else 160.0
        max_edge_distance = 50.0 if action_type == "type" else 35.0
        if described_type and edge_distance <= max_edge_distance and (
            edge_distance <= 1 or center_distance <= max_center_distance
        ):
            candidates.append((edge_distance, center_distance, area, control,
                               center_x, center_y))

    chosen: tuple[dict[str, Any], float, float] | None = None
    method = "android_control_geometry"
    if len(semantic) == 1:
        _, control, center_x, center_y = semantic[0]
        chosen = control, center_x, center_y
        method = "android_control_semantic_geometry"
    elif candidates:
        candidates.sort(key=lambda item: item[:3])
        best = candidates[0]
        if len(candidates) == 1 or not (
            abs(best[0] - candidates[1][0]) <= 4
            and abs(best[1] - candidates[1][1]) <= 4
            and math.hypot(
                best[4] - candidates[1][4], best[5] - candidates[1][5]
            ) > 4
        ):
            chosen = best[3], best[4], best[5]
    if chosen is None:
        return decision

    control, center_x, center_y = chosen
    if (
        abs(float(action.x) - center_x) <= 1
        and abs(float(action.y) - center_y) <= 1
    ):
        return decision
    grounded = action.model_copy(update={
        "x": center_x,
        "y": center_y,
        "snap": {
            "method": method,
            "original": [action.x, action.y],
            "snapped": [center_x, center_y],
            "info": str(control.get("label") or control.get("kind") or "control"),
        },
    })
    return decision.model_copy(update={"action": grounded})


__all__ = ["ground_action_to_android_control"]
