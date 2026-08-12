"""Bind visual Android coordinates to current UIAutomator controls."""

from __future__ import annotations

import math
import re
from typing import Any

from gui_agent.core.schemas import BaseActionDecision


_TAP_KINDS = {"button", "checkbox", "radio", "switch", "select", "text_input"}


def _words(value: object) -> list[str]:
    return re.findall(r"[a-z0-9]+", str(value or "").casefold())


def _described_kind_matches(description: str, kind: str) -> bool:
    words = set(_words(description))
    if words & {"input", "field", "textbox", "editor"}:
        return any(token in kind for token in ("input", "textbox", "editor"))
    if words & {"checkbox", "multiselect"} or {"multi", "select"} <= words:
        return kind == "checkbox"
    if words & {"switch", "toggle"}:
        return kind == "switch"
    return "button" not in words or kind in {"button", "checkbox", "radio", "switch"}


def _label_position(description: str, label: str) -> int | None:
    if not description or not label:
        return None
    if label.isascii():
        needle, haystack = _words(label), _words(description)
        if sum(map(len, needle)) < 3:
            return None
        width = len(needle)
        return next((
            index for index in range(len(haystack) - width + 1)
            if haystack[index:index + width] == needle
        ), None)
    needle = "".join(char.casefold() for char in label if char.isalnum())
    haystack = "".join(char.casefold() for char in description if char.isalnum())
    index = haystack.find(needle) if len(needle) >= 2 else -1
    return index if index >= 0 else None


def _action_point(control: dict[str, Any]) -> tuple[float, float] | None:
    point, rect = control.get("action_point"), control.get("rect")
    if not isinstance(point, dict) or not isinstance(rect, dict):
        return None
    if not all(
        isinstance(item.get(key), (int, float))
        for item, keys in ((point, ("x", "y")), (rect, ("x", "y", "w", "h")))
        for key in keys
    ):
        return None
    x, y = float(point["x"]), float(point["y"])
    if not (0 <= x < 1000 and 0 <= y < 1000):
        return None
    if (
        abs(x - float(rect["x"])) > max(0.0, float(rect["w"])) / 2
        or abs(y - float(rect["y"])) > max(0.0, float(rect["h"])) / 2
    ):
        return None
    return x, y


def _rect_contains(
    outer: dict[str, Any],
    inner: dict[str, Any],
    *,
    tolerance: float = 2.0,
) -> bool:
    """Whether one center-based rect geometrically contains another."""

    try:
        ox, oy = float(outer["x"]), float(outer["y"])
        ow, oh = max(0.0, float(outer["w"])), max(0.0, float(outer["h"]))
        ix, iy = float(inner["x"]), float(inner["y"])
        iw, ih = max(0.0, float(inner["w"])), max(0.0, float(inner["h"]))
    except (KeyError, TypeError, ValueError):
        return False
    return (
        ox - ow / 2 - tolerance <= ix - iw / 2
        and oy - oh / 2 - tolerance <= iy - ih / 2
        and ox + ow / 2 + tolerance >= ix + iw / 2
        and oy + oh / 2 + tolerance >= iy + ih / 2
    )


def _nested_specific_match(matches: list[tuple]) -> tuple | None:
    """Choose a unique descendant widget from same-label container controls.

    UIAutomator commonly exposes both a clickable row and its child switch with
    the same role and label. This is not a semantic ambiguity: the descendant is
    the precise hit target. Real same-label siblings remain ambiguous.
    """

    if len(matches) < 2:
        return matches[0] if matches else None
    ranked = sorted(matches, key=lambda item: item[1][2])
    candidate = ranked[0]
    candidate_control = candidate[2]
    candidate_ref = str(candidate_control.get("ref") or "")
    candidate_rect = candidate_control.get("rect")
    if not candidate_ref or not isinstance(candidate_rect, dict):
        return None
    for other in ranked[1:]:
        other_control = other[2]
        other_ref = str(other_control.get("ref") or "")
        other_rect = other_control.get("rect")
        if (
            not other_ref
            or not candidate_ref.startswith(other_ref + ".")
            or not isinstance(other_rect, dict)
            or not _rect_contains(other_rect, candidate_rect)
        ):
            return None
    return candidate


def ground_action_to_android_control(
    decision: BaseActionDecision,
    controls: list[dict[str, Any]] | None,
) -> BaseActionDecision:
    """Correct one bounded, unambiguous visual miss using current controls."""

    action = decision.action
    action_type = str(action.action_type or "")
    if action_type not in {"tap", "click", "type"} or action.x is None or action.y is None:
        return decision
    typing = action_type == "type"
    description = str(action.description or "")
    semantic: list[tuple[tuple[int, int], tuple, dict[str, Any]]] = []
    nearby: list[tuple[tuple, dict[str, Any]]] = []
    for control in controls or []:
        rect = control.get("rect") if isinstance(control, dict) else None
        kind = str(control.get("kind") or "").casefold()
        compatible = (
            any(token in kind for token in ("input", "textbox", "editor"))
            if typing else kind in _TAP_KINDS
        )
        if (
            not compatible or control.get("in_viewport") is False
            or not isinstance(rect, dict)
            or not all(isinstance(rect.get(key), (int, float)) for key in ("x", "y", "w", "h"))
            or not _described_kind_matches(description, kind)
        ):
            continue
        x, y = float(rect["x"]), float(rect["y"])
        width, height = max(0.0, float(rect["w"])), max(0.0, float(rect["h"]))
        if not (0 <= x < 1000 and 0 <= y < 1000) or (width >= 950 and height >= 600):
            continue
        center = math.hypot(float(action.x) - x, float(action.y) - y)
        edge = math.hypot(
            max(abs(float(action.x) - x) - width / 2, 0.0),
            max(abs(float(action.y) - y) - height / 2, 0.0),
        )
        max_center, max_edge = ((220.0, 50.0) if typing else (160.0, 35.0))
        if edge > max_edge or (edge > 1 and center > max_center):
            continue
        geometry = (edge, center, max(1.0, width * height), x, y)
        nearby.append((geometry, control))
        label = str(control.get("label") or "").strip()
        position = _label_position(description, label)
        if position is not None:
            semantic.append(((-position, len(label)), geometry, control))

    chosen: tuple[tuple, dict[str, Any], str] | None = None
    if semantic:
        best_score = max(item[0] for item in semantic)
        matches = [item for item in semantic if item[0] == best_score]
        match = matches[0] if len(matches) == 1 else _nested_specific_match(matches)
        if match is not None:
            _, geometry, control = match
            method = (
                "android_control_semantic_nested_geometry"
                if len(matches) > 1
                else "android_control_semantic_geometry"
            )
            chosen = geometry, control, method
    elif nearby:
        nearby.sort(key=lambda item: item[0][:3])
        geometry, control = nearby[0]
        ambiguous = len(nearby) > 1 and all(
            abs(geometry[index] - nearby[1][0][index]) <= 4 for index in (0, 1)
        ) and math.hypot(
            geometry[3] - nearby[1][0][3], geometry[4] - nearby[1][0][4]
        ) > 4
        if not ambiguous:
            chosen = geometry, control, "android_control_geometry"
    if chosen is None:
        return decision

    geometry, control, method = chosen
    edge, _, _, x, y = geometry
    preferred = (
        _action_point(control)
        if not typing and method.startswith("android_control_semantic")
        else None
    )
    if preferred is not None:
        x, y = preferred
        method = method.replace("_geometry", "_action_point")
    elif edge <= 1 and method == "android_control_geometry" and not typing:
        x, y = float(action.x), float(action.y)
    if abs(float(action.x) - x) <= 1 and abs(float(action.y) - y) <= 1:
        return decision
    grounded = action.model_copy(update={
        "x": x,
        "y": y,
        "snap": {
            "method": method,
            "original": [action.x, action.y],
            "snapped": [x, y],
            "info": str(control.get("label") or control.get("kind") or "control"),
        },
    })
    return decision.model_copy(update={"action": grounded})


__all__ = ["ground_action_to_android_control"]
