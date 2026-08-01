"""Normalize the optional Android UIAutomator hierarchy."""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from typing import Any


_BOUNDS = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
_ROLES = {
    "button": "button", "imagebutton": "button", "edittext": "textbox",
    "checkbox": "checkbox", "radiobutton": "radio", "switch": "switch",
    "switchcompat": "switch", "togglebutton": "switch", "spinner": "combobox",
    "recyclerview": "list", "listview": "list", "textview": "text",
    "imageview": "img",
}
_COLLECTION_CLASSES = {"recyclerview", "listview", "gridview"}


def _role(class_name: str, *, clickable: bool, scrollable: bool) -> str:
    name = str(class_name or "").rpartition(".")[2].casefold()
    if name in _ROLES and name != "textview":
        return _ROLES[name]
    if scrollable or name in {"scrollview", "horizontalscrollview"}:
        return "region"
    if clickable:
        return "button"
    if name in _ROLES:
        return _ROLES[name]
    return "group"


def _label(node: ET.Element) -> str:
    attrs = node.attrib
    return (
        str(attrs.get("content-desc") or attrs.get("text") or "").strip()
        or str(attrs.get("resource-id") or "").rpartition("/")[2].strip()
    )


def _digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _short_class(node: ET.Element) -> str:
    return str(node.attrib.get("class") or "").rpartition(".")[2]


def _short_resource(node: ET.Element) -> str:
    return str(node.attrib.get("resource-id") or "").rpartition("/")[2]


def _raw_bounds(node: ET.Element) -> tuple[int, int, int, int] | None:
    match = _BOUNDS.fullmatch(str(node.attrib.get("bounds") or "").strip())
    if match is None:
        return None
    values = tuple(map(int, match.groups()))
    return values if values[2] > values[0] and values[3] > values[1] else None


def _shape(node: ET.Element) -> list[Any]:
    attrs = node.attrib
    return [
        _short_class(node), _short_resource(node),
        attrs.get("clickable") == "true",
        attrs.get("checkable") == "true",
        attrs.get("scrollable") == "true",
        bool(str(attrs.get("text") or "").strip()),
        bool(str(attrs.get("content-desc") or "").strip()),
        [_shape(child) for child in node if child.tag == "node"],
    ]


def _content(node: ET.Element) -> list[Any]:
    attrs = node.attrib
    return [
        _short_class(node), _short_resource(node),
        str(attrs.get("text") or "").strip(),
        str(attrs.get("content-desc") or "").strip(),
        attrs.get("checked"), attrs.get("selected"), attrs.get("enabled"),
        [_content(child) for child in node if child.tag == "node"],
    ]


def _texts(node: ET.Element) -> list[str]:
    result: list[str] = []
    for child in node.iter("node"):
        for name in ("content-desc", "text"):
            value = " ".join(str(child.attrib.get(name) or "").split())
            if value and value not in result:
                result.append(value)
    return result


def _controls(node: ET.Element, path: tuple[int, ...]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []

    def walk(current: ET.Element, current_path: tuple[int, ...]) -> None:
        attrs = current.attrib
        if attrs.get("clickable") == "true" or attrs.get("checkable") == "true":
            labels = _texts(current)
            result.append({
                "ref": "android:" + ".".join(map(str, current_path)),
                "role": _role(
                    str(attrs.get("class") or ""),
                    clickable=attrs.get("clickable") == "true",
                    scrollable=attrs.get("scrollable") == "true",
                ),
                "label": labels[0] if labels else _short_resource(current),
                **(
                    {"value": attrs.get("checked") == "true"}
                    if attrs.get("checkable") == "true" else {}
                ),
            })
        for index, child in enumerate(current):
            if child.tag == "node":
                walk(child, (*current_path, index))

    walk(node, path)
    return result


def _normalized_bounds(
    bounds: tuple[int, int, int, int] | None,
    viewport_size: tuple[int, int],
) -> tuple[float, float, float, float] | None:
    if bounds is None:
        return None
    width, height = viewport_size
    return (
        bounds[0] * 1000 / width, bounds[1] * 1000 / height,
        bounds[2] * 1000 / width, bounds[3] * 1000 / height,
    )


def _nodes(
    parent: ET.Element, path: tuple[int, ...] = (), depth: int = 0,
) -> Iterator[tuple[ET.Element, tuple[int, ...], int]]:
    for index, child in enumerate(parent):
        child_path = (*path, index)
        if child.tag == "node":
            yield child, child_path, depth
        yield from _nodes(child, path=child_path, depth=depth + 1)


def semantic_tree_from_uiautomator(
    xml_text: str | None,
    *,
    viewport_size: tuple[int, int],
) -> list[dict[str, Any]] | None:
    """Return current-frame semantic nodes; ``None`` means the optional sensor failed."""
    if not str(xml_text or "").strip():
        return None
    try:
        root = ET.fromstring(str(xml_text))
    except ET.ParseError:
        return None
    width, height = viewport_size
    if width <= 0 or height <= 0:
        return None

    result: list[dict[str, Any]] = []
    for node, path, depth in _nodes(root):
        attrs = node.attrib
        clickable = attrs.get("clickable") == "true"
        scrollable = attrs.get("scrollable") == "true"
        key = _label(node)
        match = _BOUNDS.fullmatch(str(attrs.get("bounds") or "").strip())
        if not key and not clickable and not scrollable:
            continue
        item: dict[str, Any] = {
            "role": _role(
                str(attrs.get("class") or ""),
                clickable=clickable,
                scrollable=scrollable,
            ),
            "key": key or "scrollable region",
            "ref": "android:" + ".".join(str(index) for index in path),
            "depth": depth,
            "scrollable": scrollable,
        }
        if match is not None:
            x1, y1, x2, y2 = map(int, match.groups())
            if x2 > x1 and y2 > y1:
                item["in_viewport"] = x2 > 0 and y2 > 0 and x1 < width and y1 < height
                item["point"] = {
                    "x": (x1 + x2) * 500 / width,
                    "y": (y1 + y2) * 500 / height,
                }
                item["rect"] = {
                    **item["point"],
                    "width": (x2 - x1) * 1000 / width,
                    "height": (y2 - y1) * 1000 / height,
                }
        class_name = str(attrs.get("class") or "").casefold()
        if attrs.get("checkable") == "true" or any(
            name in class_name for name in ("checkbox", "switch", "togglebutton")
        ):
            item["value"] = attrs.get("checked") == "true"
        elif class_name.endswith("edittext"):
            item["value"] = str(attrs.get("text") or "")
        if clickable and "selected" in attrs:
            item["selected"] = attrs.get("selected") == "true"
        elif attrs.get("selected") == "true":
            item["selected"] = True
        result.append(item)
    return result


def collection_regions_from_uiautomator(
    xml_text: str | None,
    *,
    viewport_size: tuple[int, int],
) -> list[dict[str, Any]] | None:
    """Expose ordered collection cells without inferring logical records."""
    if not str(xml_text or "").strip() or min(viewport_size) <= 0:
        return None
    try:
        root = ET.fromstring(str(xml_text))
    except ET.ParseError:
        return None

    candidates = [
        (node, path, depth)
        for node, path, depth in _nodes(root)
        if _short_class(node).casefold() in _COLLECTION_CLASSES
        and any(child.tag == "node" for child in node)
    ]
    # UI frameworks sometimes wrap one RecyclerView in another with identical bounds.
    # Keep the deepest/richest representative, while preserving genuinely distinct regions.
    selected: dict[object, tuple[ET.Element, tuple[int, ...], int]] = {}
    for candidate in candidates:
        node, path, depth = candidate
        key: object = _raw_bounds(node) or ("path", path)
        previous = selected.get(key)
        if previous is None or (len(node), depth) > (len(previous[0]), previous[2]):
            selected[key] = candidate

    regions: list[dict[str, Any]] = []
    for node, path, _depth in selected.values():
        raw_region_bounds = _raw_bounds(node)
        cells: list[dict[str, Any]] = []
        for index, child in enumerate(child for child in node if child.tag == "node"):
            raw_cell_bounds = _raw_bounds(child)
            child_path = (*path, index)
            cells.append({
                "ref": "android:" + ".".join(map(str, child_path)),
                "structural_key": _digest(_shape(child)),
                "content_key": _digest(_content(child)),
                "class_name": _short_class(child),
                "resource": _short_resource(child),
                "bounds": _normalized_bounds(raw_cell_bounds, viewport_size),
                "texts": _texts(child),
                "controls": _controls(child, child_path),
                "clipped_top": bool(
                    raw_cell_bounds and raw_region_bounds
                    and raw_cell_bounds[1] <= raw_region_bounds[1]
                ),
                "clipped_bottom": bool(
                    raw_cell_bounds and raw_region_bounds
                    and raw_cell_bounds[3] >= raw_region_bounds[3]
                ),
            })
        resource = _short_resource(node)
        identity: list[Any] = [_short_class(node), resource]
        if not resource:
            identity.append(list(path))
        regions.append({
            "ref": "android-collection:" + ".".join(map(str, path)),
            "surface_fingerprint": "android-collection:" + _digest(identity),
            "cells": cells,
            "bounds": _normalized_bounds(raw_region_bounds, viewport_size),
            "traversal": {"type": "scroll"},
        })
    return regions


__all__ = [
    "collection_regions_from_uiautomator",
    "semantic_tree_from_uiautomator",
]
