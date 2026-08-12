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
_PERSISTENCE_WORDS = {"save", "send", "submit", "publish", "post"}
_GENERIC_CONTROL_WORDS = {
    "button", "checkbox", "control", "radio", "switch", "toggle", "widget",
}


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


def _webview_stream(
    root: ET.Element,
) -> tuple[ET.Element, tuple[int, ...], int, str] | None:
    """Return the richest repeated child stream from the active WebView."""
    webviews = [
        item for item in _nodes(root)
        if _short_class(item[0]).casefold() == "webview"
        and item[0].attrib.get("scrollable") == "true"
    ]
    if not webviews:
        return None
    webview, path, depth = webviews[-1]
    streams: list[tuple[int, int, int, ET.Element, tuple[int, ...], str]] = []
    captions = {path: _label(webview)}
    for node, node_path, node_depth in _nodes(webview, path, depth + 1):
        caption = _label(node) or captions.get(node_path[:-1], "")
        captions[node_path] = caption
        children = [child for child in node if child.tag == "node"]
        textual = sum(bool(_texts(child)) for child in children)
        if len(children) >= 2 and textual >= 2:
            streams.append((
                textual, len(children), node_depth, node, node_path, caption,
            ))
    if not streams:
        return None
    _, _, depth, node, path, caption = max(streams, key=lambda item: item[:3])
    return node, path, depth, caption


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
        visible_label = str(
            attrs.get("content-desc") or attrs.get("text") or ""
        ).strip()
        if clickable and not visible_label:
            descendant_labels = _texts(node)
            visible_label = descendant_labels[0] if descendant_labels else ""
        resource = _short_resource(node)
        key = visible_label or resource
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
            "clickable": clickable,
        }
        if resource:
            item["resource"] = resource
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


def _ref_parts(node: dict[str, Any]) -> tuple[str, ...]:
    return tuple(str(node.get("ref") or "").removeprefix("android:").split("."))


def _shared_ref_depth(left: dict[str, Any], right: dict[str, Any]) -> int:
    depth = 0
    for left_part, right_part in zip(_ref_parts(left), _ref_parts(right)):
        if left_part != right_part:
            break
        depth += 1
    return depth


def _is_generic_control_label(value: str) -> bool:
    words = set(filter(None, re.split(r"[_\W]+", value.casefold())))
    return not words or words.issubset(_GENERIC_CONTROL_WORDS)


def _nearby_control_label(
    control: dict[str, Any],
    nodes: list[dict[str, Any]],
) -> str:
    """Associate an unlabeled control with text in the same hierarchy row.

    Android Settings and many Material layouts expose a Switch whose own only
    identity is ``switch_widget`` while its visible label is a sibling TextView.
    Prefer the closest vertically aligned text with the deepest shared hierarchy
    ancestry; never borrow a label from another distant row.
    """

    target = control.get("rect")
    if not isinstance(target, dict):
        return ""
    target_x = float(target["x"])
    target_y = float(target["y"])
    target_h = float(target["height"])
    candidates: list[tuple[int, float, float, str]] = []
    for node in nodes:
        if node is control or node.get("in_viewport") is False:
            continue
        label = str(node.get("key") or "").strip()
        rect = node.get("rect")
        if (
            not label
            or _is_generic_control_label(label)
            or not isinstance(rect, dict)
            or node.get("role") not in {"text", "button"}
        ):
            continue
        shared_depth = _shared_ref_depth(control, node)
        if shared_depth < 2:
            continue
        label_x = float(rect["x"])
        label_y = float(rect["y"])
        label_h = float(rect["height"])
        vertical_distance = abs(label_y - target_y)
        row_tolerance = max(35.0, (target_h + label_h) * 0.75)
        if vertical_distance > row_tolerance or label_x >= target_x:
            continue
        candidates.append((
            shared_depth,
            -vertical_distance,
            -abs(target_x - label_x),
            label,
        ))
    return max(candidates)[-1] if candidates else ""


def form_controls_from_semantic_tree(
    nodes: list[dict[str, Any]] | None,
) -> list[dict[str, Any]] | None:
    """Project current actionable Android controls with normalized geometry.

    UIAutomator commonly puts a menu row's visible text on a child TextView and
    ``clickable=true`` on its parent layout.  The semantic tree carries that
    parent identity, so expose it to enhanced workers just like text fields and
    switches.  ``rect.x/y`` are always the center in the shared 0-1000 space;
    ``bounds`` retains edge coordinates for consumers that need them.
    """
    if nodes is None:
        return None
    controls: list[dict[str, Any]] = []
    for node in nodes:
        resource = str(node.get("resource") or "")
        key = str(node.get("key") or "")
        identity_words = set(filter(
            None, re.split(r"[_\W]+", f"{resource} {key}".casefold()),
        ))
        role = str(node.get("role") or "")
        persistence = bool(identity_words & _PERSISTENCE_WORDS)
        kind = (
            "text_input" if role == "textbox"
            else "switch" if role in {"checkbox", "radio", "switch"}
            else "select" if role == "button" and identity_words & {"date", "time"}
            else "button" if role == "button" and (
                persistence or node.get("clickable") is True
            )
            else ""
        )
        if not kind:
            continue
        own_label = key if not _is_generic_control_label(key) else ""
        label = own_label or _nearby_control_label(node, nodes)
        if not label:
            label = resource.replace("_", " ") or key
        item: dict[str, Any] = {
            "label": label,
            "ref": str(node.get("ref") or ""),
            "kind": kind,
            "value": node.get("value", node.get("key", "")),
            "resource": resource,
        }
        if persistence:
            node["role"] = "button"
            node["form_action"] = "commit"
            item["form_action"] = "commit"
        if "in_viewport" in node:
            item["in_viewport"] = node["in_viewport"]
        rect = node.get("rect")
        if isinstance(rect, dict):
            x, y = float(rect["x"]), float(rect["y"])
            width, height = float(rect["width"]), float(rect["height"])
            if kind == "button" and width >= 950 and height >= 600:
                # Ignore page-sized clickable wrappers. Full-width menu rows
                # remain useful because their height is small.
                continue
            item["bounds"] = (x - width / 2, y - height / 2,
                              x + width / 2, y + height / 2)
            item["rect"] = {
                "x": x, "y": y,
                "w": width, "h": height,
            }
        controls.append(item)

    # Some UI toolkits mark both a row and its immediate child clickable. Keep
    # the smaller rendered target when they expose the same visible identity.
    deduplicated: list[dict[str, Any]] = []
    for control in controls:
        label = "".join(
            character.casefold()
            for character in str(control.get("label") or "")
            if character.isalnum()
        )
        rect = control.get("rect")
        duplicate_index: int | None = None
        if label and isinstance(rect, dict):
            for index, existing in enumerate(deduplicated):
                existing_rect = existing.get("rect")
                if (
                    existing.get("kind") == control.get("kind")
                    and "".join(
                        character.casefold()
                        for character in str(existing.get("label") or "")
                        if character.isalnum()
                    ) == label
                    and isinstance(existing_rect, dict)
                    and abs(float(existing_rect["x"]) - float(rect["x"])) <= 8
                    and abs(float(existing_rect["y"]) - float(rect["y"])) <= 8
                ):
                    duplicate_index = index
                    break
        if duplicate_index is None:
            deduplicated.append(control)
            continue
        existing = deduplicated[duplicate_index]
        existing_rect = existing["rect"]
        if float(rect["w"]) * float(rect["h"]) < (
            float(existing_rect["w"]) * float(existing_rect["h"])
        ):
            deduplicated[duplicate_index] = control
    return deduplicated


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
    web_fallbacks: dict[tuple[int, ...], str] = {}
    fallback = _webview_stream(root) if not candidates else None
    if fallback is not None:
        node, path, depth, caption = fallback
        candidates.append((node, path, depth))
        web_fallbacks[path] = caption
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
        seen_cells: set[tuple[tuple[int, int, int, int], str]] = set()
        for index, child in enumerate(child for child in node if child.tag == "node"):
            raw_cell_bounds = _raw_bounds(child)
            child_path = (*path, index)
            texts = _texts(child)
            controls = _controls(child, child_path)
            if not texts and not controls:
                continue
            content_key = _digest([
                texts,
                [
                    [control.get(key) for key in ("role", "label", "value")]
                    for control in controls
                ],
            ])
            if raw_cell_bounds is not None:
                identity = (raw_cell_bounds, content_key)
                if identity in seen_cells:
                    continue
                seen_cells.add(identity)
            cells.append({
                "ref": "android:" + ".".join(map(str, child_path)),
                "structural_key": _digest(_shape(child)),
                "content_key": content_key,
                "class_name": _short_class(child),
                "resource": _short_resource(child),
                "bounds": _normalized_bounds(raw_cell_bounds, viewport_size),
                "texts": texts,
                "controls": controls,
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
        page_caption = web_fallbacks.get(path, "")
        identity: list[Any] = [_short_class(node), resource]
        if path in web_fallbacks or not resource:
            # Surface identity never includes current records or their count.
            identity.append(page_caption if path in web_fallbacks else list(path))
        regions.append({
            "ref": "android-collection:" + ".".join(map(str, path)),
            "surface_fingerprint": "android-collection:" + _digest(identity),
            "cells": cells,
            "bounds": _normalized_bounds(raw_region_bounds, viewport_size),
            "caption": page_caption,
            "traversal": (
                {"type": "scroll"}
                if node.attrib.get("scrollable") == "true" else {"type": "static"}
            ),
        })
    return regions


__all__ = [
    "collection_regions_from_uiautomator",
    "form_controls_from_semantic_tree",
    "semantic_tree_from_uiautomator",
]
