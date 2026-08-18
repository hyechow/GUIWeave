"""Browser semantic page tree: CDP Accessibility → pruned flat KV tree.

Replaces the scattered read_tables / read_form_controls / dom_state sensors with
a single authoritative representation of the page's interactive and informational
structure, independent of scroll position.

Each node in the output is::

    {
        "role":  str,   # ARIA role (button, textbox, heading, link, ...)
        "key":   str,   # accessible name — the human-readable label
        "value": str,   # current value for form controls; "" otherwise
        "ref":   int,   # backendDOMNodeId — use for DOM-direct click/scroll
        "depth": int,   # nesting depth (0 = page root)
        "point": {"x": float, "y": float},  # normalized center; may be outside 0..1000
        "in_viewport": bool,
    }

This is intentionally a flat list, not a recursive tree.  The LLM mapper reads
the list top-to-bottom; depth gives it enough hierarchy to resolve ambiguity
without needing to reconstruct the tree.
"""

from __future__ import annotations

from typing import Any

# ARIA roles that are purely presentational — prune unconditionally.
# InlineTextBox: sub-text fragments of links/buttons (the parent already holds the full name).
_SKIP_ROLES = frozenset({
    "none", "presentation", "generic", "group", "Section",
    "Inline", "LineBreak", "StaticText", "InlineTextBox",
})

# Interactive/data roles: keep regardless of whether they have an accessible name.
_ALWAYS_KEEP = frozenset({
    "button", "link", "textbox", "searchbox", "combobox", "listbox",
    "option", "checkbox", "radio", "slider", "spinbutton",
    "menuitem", "menuitemcheckbox", "menuitemradio", "switch",
    "tab", "treeitem",
    # Data
    "table", "grid", "treegrid", "row", "cell", "columnheader",
    "rowheader", "gridcell",
    # Heading/image are only useful when they have a name
    "heading", "img",
})

# Structural/landmark roles: only keep when they have a non-empty accessible name.
# Without a name they are transparent wrappers that add noise without adding orientation.
_KEEP_IF_NAMED = frozenset({
    "figure", "separator",
    "banner", "navigation", "main", "complementary", "contentinfo",
    "form", "search", "region", "dialog", "alertdialog", "alert",
    "status", "log", "timer",
    "menubar", "menu", "tablist", "tabpanel", "toolbar",
    "tree", "listbox", "feed", "list", "listitem",
})


def _prop_value(properties: list[dict], name: str) -> str:
    """Extract a named property value from an AX node's properties list."""
    for p in properties:
        if p.get("name") == name:
            v = p.get("value", {})
            raw = v.get("value")
            if raw is not None:
                return str(raw)
    return ""


def _walk(
    node_id: str,
    nodes: dict[str, dict],
    depth: int,
    out: list[dict],
) -> None:
    node = nodes.get(node_id)
    if node is None:
        return

    role = (node.get("role") or {}).get("value", "")
    name = ((node.get("name") or {}).get("value") or "").strip()
    properties: list[dict] = node.get("properties") or []
    ref: int = node.get("backendDOMNodeId") or 0

    skip = (
        node.get("ignored") is True
        or role in _SKIP_ROLES
        or (role not in _ALWAYS_KEEP and role not in _KEEP_IF_NAMED)
        or (role in _KEEP_IF_NAMED and not name)
        or (role == "img" and not name)   # decorative image
    )

    if not skip:
        value = _prop_value(properties, "value")
        # For checkboxes/radios the interesting value is "checked" state.
        if role in {"checkbox", "radio", "switch"}:
            checked = _prop_value(properties, "checked")
            if checked:
                value = checked
        # For links, expose the href URL (Chrome AX tree includes "url" property).
        url = _prop_value(properties, "url") if role == "link" else ""
        out.append({
            "role": role,
            "key": name,
            "value": value,
            "url": url,
            "ref": ref,
            "depth": depth,
        })
        next_depth = depth + 1
    else:
        next_depth = depth  # collapsed; children inherit parent depth

    for child_id in node.get("childIds") or []:
        _walk(child_id, nodes, next_depth, out)


def build_semantic_tree(cdp_send: Any) -> list[dict]:
    """Fetch the full AX tree via CDP and return a pruned flat node list.

    ``cdp_send`` is the device's ``_cdp_send(method, params) -> dict`` callable.
    Returns [] on any failure so callers can treat it as optional enrichment.
    """
    try:
        res = cdp_send("Accessibility.getFullAXTree", {})
    except Exception:
        return []

    raw_nodes: list[dict] = (res or {}).get("nodes") or []
    if not raw_nodes:
        return []

    # Build lookup map: nodeId → raw node.
    node_map: dict[str, dict] = {n["nodeId"]: n for n in raw_nodes if "nodeId" in n}

    # Find root(s): nodes that are not referenced as a child of any other node.
    all_children: set[str] = set()
    for n in raw_nodes:
        all_children.update(n.get("childIds") or [])
    roots = [n["nodeId"] for n in raw_nodes if n["nodeId"] not in all_children]

    out: list[dict] = []
    for root_id in roots:
        _walk(root_id, node_map, 0, out)

    _attach_viewport_points(cdp_send, out)
    return out


def _attach_viewport_points(cdp_send: Any, tree: list[dict]) -> None:
    """Join AX refs to current viewport centers from one DOM layout snapshot."""
    try:
        snapshot = cdp_send("DOMSnapshot.captureSnapshot", {"computedStyles": []})
        metrics = cdp_send("Page.getLayoutMetrics", {})
        document = (snapshot.get("documents") or [])[0]
        node_refs = document["nodes"]["backendNodeId"]
        layout = document["layout"]
        viewport = (
            metrics.get("cssVisualViewport")
            or metrics.get("visualViewport")
            or {}
        )
        page_x = float(viewport.get("pageX") or 0)
        page_y = float(viewport.get("pageY") or 0)
        width = float(viewport["clientWidth"])
        height = float(viewport["clientHeight"])
    except Exception:
        return
    if width <= 0 or height <= 0:
        return

    boxes: dict[int, list[float]] = {}
    for node_index, bounds in zip(layout.get("nodeIndex") or [], layout.get("bounds") or []):
        if (
            isinstance(node_index, int)
            and 0 <= node_index < len(node_refs)
            and isinstance(bounds, list)
            and len(bounds) == 4
        ):
            boxes[int(node_refs[node_index])] = bounds
    for node in tree:
        bounds = boxes.get(int(node.get("ref") or 0))
        if bounds is None:
            continue
        x, y, box_width, box_height = map(float, bounds)
        center_x = x - page_x + box_width / 2
        center_y = y - page_y + box_height / 2
        in_viewport = 0 <= center_x < width and 0 <= center_y < height
        node["in_viewport"] = in_viewport
        node["point"] = {
            "x": center_x / width * 1000,
            "y": center_y / height * 1000,
        }
