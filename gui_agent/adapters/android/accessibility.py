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
_COMMIT_LABELS = {"apply", "confirm", "create", "done", "publish", "save", "send", "submit"}
_PRIVATE_USE_SELECTION_STATES = {
    "\U000F05E0": True,
    "\U000F0766": False,
}
_GENERIC_CONTROL_WORDS = {
    "button", "checkbox", "control", "radio", "switch", "toggle", "widget",
}
_RESOURCE_NOISE = set("""
    action actions active button checked collapsed container control disabled display
    draft edittext enabled expanded false field form header input item layout list menu
    options post quick row screen selected text toggled true unchecked view widget
""".split())
_ICON_GLYPH_RE = re.compile(r"[\uE000-\uF8FF\U000F0000-\U000FFFFD]")


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


def _visible_text(value: Any) -> str:
    """Drop icon-font glyphs from textual labels, retaining icon-only labels."""
    compact = " ".join(str(value or "").split())
    if not _ICON_GLYPH_RE.search(compact):
        return compact
    textual = _ICON_GLYPH_RE.sub(" ", compact)
    textual = re.sub(r"(?:\s*[,|·•]\s*)+", " ", textual).strip()
    return textual if any(char.isalnum() for char in textual) else compact


def _private_use_selection_state(value: Any) -> bool | None:
    states = {
        _PRIVATE_USE_SELECTION_STATES[character]
        for character in str(value or "")
        if character in _PRIVATE_USE_SELECTION_STATES
    }
    return states.pop() if len(states) == 1 else None


def _resource_visible_label(resource: str, *, include_root: bool = True) -> str:
    segments = str(resource or "").casefold().split(".")
    for index, segment in enumerate(reversed(segments)):
        meaningful = [
            word for word in re.split(r"[_\W]+", segment)
            if word and word not in _RESOURCE_NOISE
        ]
        if meaningful and (
            include_root or len(segments) == 1 or index < len(segments) - 1
        ):
            return " ".join(meaningful)
    return ""


def _texts(node: ET.Element) -> list[str]:
    result: list[str] = []
    for child in node.iter("node"):
        for name in ("content-desc", "text"):
            value = _visible_text(child.attrib.get(name))
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
        raw_visible_label = attrs.get("content-desc") or attrs.get("text") or ""
        visible_label = _visible_text(raw_visible_label)
        glyph_selection_state = _private_use_selection_state(raw_visible_label)
        resource = _short_resource(node)
        if clickable and not visible_label:
            descendant_labels = _texts(node)
            visible_label = (
                _resource_visible_label(resource)
                or (descendant_labels[0] if descendant_labels else "")
            )
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
        if clickable and glyph_selection_state is not None:
            # Some icon-font UIs expose multi-select state only through stable
            # checked/unchecked glyphs in the clickable row's description.
            item["glyph_selection_state"] = glyph_selection_state
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
    _infer_private_use_selection_states(result)
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
    *,
    label_on_right: bool = False,
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
    candidates: list[tuple[int, float, float, float, str]] = []
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
        wrong_side = label_x <= target_x if label_on_right else label_x >= target_x
        if vertical_distance > row_tolerance or wrong_side:
            continue
        distances = (
            (-int(vertical_distance / 10), -abs(target_x - label_x), -vertical_distance)
            if label_on_right
            else (-vertical_distance, -abs(target_x - label_x), 0.0)
        )
        candidates.append((
            shared_depth, *distances, label,
        ))
    return max(candidates)[-1] if candidates else ""


def _repeated_leading_choices(nodes: list[dict[str, Any]]) -> dict[str, str]:
    """Name repeated compact, unlabeled selectors at the start of list rows."""

    candidates: list[tuple[dict[str, Any], str]] = []
    for node in nodes:
        rect = node.get("rect")
        if (
            node.get("role") != "button"
            or node.get("key") != "scrollable region"
            or node.get("in_viewport") is False
            or not isinstance(rect, dict)
            or float(rect["x"]) >= 250
            or float(rect["width"]) > 140
            or float(rect["height"]) > 100
        ):
            continue
        label = _nearby_control_label(node, nodes, label_on_right=True)
        if label:
            candidates.append((node, label))

    repeated: dict[str, str] = {}
    for node, label in candidates:
        rect = node["rect"]
        if any(
            other is not node
            and _shared_ref_depth(node, other) >= 2
            and abs(float(other["rect"]["x"]) - float(rect["x"])) <= 25
            and abs(float(other["rect"]["width"]) - float(rect["width"])) <= 25
            and abs(float(other["rect"]["height"]) - float(rect["height"])) <= 25
            for other, _ in candidates
        ):
            repeated[str(node.get("ref") or "")] = label
    return repeated


def _private_use_action_point(
    control: dict[str, Any],
    nodes: list[dict[str, Any]],
) -> dict[str, float] | None:
    """Locate a trailing icon affordance inside one wide clickable row."""

    rect = control.get("rect")
    ref = str(control.get("ref") or "")
    if not isinstance(rect, dict) or not ref or float(rect.get("width") or 0) < 600:
        return None
    center_x = float(rect["x"])
    center_y = float(rect["y"])
    width = float(rect["width"])
    height = float(rect["height"])
    descendants: list[tuple[float, dict[str, float]]] = []
    for node in nodes:
        point = node.get("point")
        if (
            not str(node.get("ref") or "").startswith(ref + ".")
            or node.get("in_viewport") is False
            or any(char.isalnum() for char in str(node.get("key") or ""))
            or _private_use_selection_state(node.get("key"))
            != control.get("glyph_selection_state")
            or not isinstance(point, dict)
            or not all(isinstance(point.get(key), (int, float)) for key in ("x", "y"))
        ):
            continue
        x, y = float(point["x"]), float(point["y"])
        if x < center_x + width / 4 or abs(y - center_y) > max(35, height / 2):
            continue
        descendants.append((x, {"x": x, "y": y}))
    return max(descendants, default=(0, None), key=lambda item: item[0])[1]


def _infer_private_use_selection_states(nodes: list[dict[str, Any]]) -> None:
    """Promote only verified glyph-backed rows to checkbox controls."""

    for node in nodes:
        selected = node.get("glyph_selection_state")
        if selected is None:
            continue
        action_point = _private_use_action_point(node, nodes)
        node.pop("glyph_selection_state", None)
        if action_point is None:
            continue
        node.update(
            role="checkbox", selection_mode="multiple",
            action_point=action_point, selected=bool(selected), value=bool(selected),
        )


def _is_commit_control(*, role: str, key: str, resource: str) -> bool:
    """Recognize explicit submission controls without matching container paths."""

    if role != "button":
        return False
    label_words = tuple(filter(None, re.split(r"[_\W]+", key.casefold())))
    resource_words = set(filter(None, re.split(r"[_\W]+", resource.casefold())))
    explicit_label = bool(
        label_words
        and (
            label_words == ("create",)
            or label_words[0] in (_COMMIT_LABELS - {"create"})
        )
    )
    commit_words = _COMMIT_LABELS - {"create"}
    return bool(
        explicit_label
        or (
            "selected" in resource_words
            and resource_words & {"action", "button"}
        )
        or (
            resource_words & commit_words
            and resource_words & {"action", "button"}
        )
    )


def _control_geometry(
    control: dict[str, Any],
) -> tuple[str, int, float, float, float, float] | None:
    rect = control.get("rect")
    ref = str(control.get("ref") or "")
    if control.get("in_viewport") is False or not isinstance(rect, dict) or not ref:
        return None
    return (
        ref, len(_ref_parts(control)),
        *(float(rect[key]) for key in ("x", "y", "w", "h")),
    )


def _occluded_control(
    control: dict[str, Any], controls: list[dict[str, Any]],
) -> bool:
    """Detect clipped controls or controls covered by a sibling/fixed layer."""

    geometry = _control_geometry(control)
    if geometry is None:
        return False
    ref, depth, x, y, width, height = geometry
    trailing = 120 <= width <= 400 and x + width / 2 >= 995
    if trailing and height < 60:
        return True
    for other in controls:
        other_geometry = _control_geometry(other)
        if other is control or other_geometry is None:
            continue
        other_ref, other_depth, ox, oy, ow, oh = other_geometry
        if (
            ref.startswith(other_ref + ".")
            or other_ref.startswith(ref + ".")
        ):
            continue
        shared_depth = _shared_ref_depth(control, other)
        if (
            other_depth < depth
            and shared_depth < min(depth, other_depth) - 1
            and abs(x - ox) < ow / 2
            and abs(y - oy) < oh / 2
        ):
            return True
        if trailing and shared_depth >= min(depth, other_depth) - 3 and (
            ow * oh < width * height
            and x - width / 2 <= ox - ow / 2
            and x + width / 2 >= ox + ow / 2
            and y - height / 2 <= oy - oh / 2
            and y + height / 2 >= oy + oh / 2
        ):
            return True
    return False


def form_controls_from_semantic_tree(
    nodes: list[dict[str, Any]] | None,
) -> list[dict[str, Any]] | None:
    """Project actionable controls with center-based normalized geometry."""
    if nodes is None:
        return None
    controls: list[dict[str, Any]] = []
    leading_choices = _repeated_leading_choices(nodes)
    for node in nodes:
        resource = str(node.get("resource") or "")
        key = str(node.get("key") or "")
        identity_words = set(filter(
            None, re.split(r"[_\W]+", f"{resource} {key}".casefold()),
        ))
        role = str(node.get("role") or "")
        persistence = _is_commit_control(
            role=role,
            key=key,
            resource=resource,
        )
        ref = str(node.get("ref") or "")
        inferred_choice = leading_choices.get(ref, "")
        kind = (
            "text_input" if role == "textbox"
            else "select" if role == "combobox"
            else "checkbox" if role == "checkbox"
            else "radio" if role == "radio"
            else "switch" if role == "switch"
            else "select" if role == "button" and identity_words & {"date", "time"}
            else "checkbox" if inferred_choice
            else "button" if role == "button" and (
                persistence or (
                    node.get("clickable") is True and key != "scrollable region"
                )
            )
            else ""
        )
        if not kind or (
            kind == "button"
            and any(
                str(child.get("ref") or "").startswith(ref + ".")
                and child.get("role") in {"textbox", "checkbox", "radio", "switch"}
                for child in nodes
            )
        ):
            continue
        resource_label = _resource_visible_label(resource, include_root=False)
        own_label = (
            resource_label
            if resource_label and (
                kind == "text_input" or not any(char.isalnum() for char in key)
            )
            else key if not _is_generic_control_label(key)
            else ""
        )
        label = inferred_choice or own_label or _nearby_control_label(node, nodes)
        if not label:
            label = resource.replace("_", " ") or key
        item: dict[str, Any] = {
            "label": label,
            "ref": ref,
            "kind": kind,
            "value": node.get("value", node.get("key", "")),
            "resource": resource,
        }
        for field in ("selected", "selection_mode", "action_point"):
            if field in node:
                item[field] = node[field]
        if inferred_choice:
            item["selection_mode"] = "multiple"
            item.pop("selected", None)
            item.pop("value", None)
        if kind == "text_input" and identity_words & {"search", "filter", "query"}:
            item["is_filter"] = True
        if persistence:
            item["form_action"] = "commit"
        if "in_viewport" in node:
            item["in_viewport"] = node["in_viewport"]
        rect = node.get("rect")
        if isinstance(rect, dict):
            x, y = float(rect["x"]), float(rect["y"])
            width, height = float(rect["width"]), float(rect["height"])
            item["bounds"] = (x - width / 2, y - height / 2,
                              x + width / 2, y + height / 2)
            item["rect"] = {
                "x": x, "y": y,
                "w": width, "h": height,
            }
        controls.append(item)

    controls = [
        control for control in controls
        if not _occluded_control(control, controls)
    ]

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
