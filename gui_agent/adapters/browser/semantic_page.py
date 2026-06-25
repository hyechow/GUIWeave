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
        role in _SKIP_ROLES
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


def _normalize(s: str) -> str:
    """Normalize a label/field name for fuzzy matching: lowercase, collapse whitespace,
    strip leading sort indicators (↑↓), strip trailing punctuation."""
    import re
    s = s.strip().lstrip("↑↓ ").lower()
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# Roles whose `value` field holds the current user-entered / selected value.
_FORM_ROLES = frozenset({
    "textbox", "searchbox", "combobox", "listbox", "spinbutton",
    "slider", "checkbox", "radio", "switch",
})

# Roles that mark a row as a filter row (skip during grid extraction).
_FILTER_ROLES = frozenset({"textbox", "searchbox", "combobox", "spinbutton"})


def _row_dedup_key(row: dict[str, str]) -> str:
    return "|".join(f"{k}={v}" for k, v in sorted(row.items()))


def read_grid_from_tree(
    tree: list[dict],
    returns: list[str],
) -> list[dict[str, str]] | None:
    """Extract all table rows from the semantic tree into a list of {field: value} dicts.

    Returns ``None`` when no table is found or no requested column can be matched
    (caller should fall back to a visual / interactive read).
    Returns ``[]`` when a table exists but contains no data rows.
    Returns the row dicts otherwise (one dict per data row, keyed by ``returns`` field names).

    Column matching uses the same two-tier normalized fuzzy logic as ``read_from_tree``.
    Filter rows (cells that contain form-control children) are skipped automatically.
    """
    import re as _re
    from difflib import SequenceMatcher

    # ── locate the table ──────────────────────────────────────────────────────
    table_idx = next((i for i, n in enumerate(tree) if n["role"] == "table"), None)
    if table_idx is None:
        return None

    table_depth = tree[table_idx]["depth"]
    row_depth = table_depth + 1
    cell_depth = table_depth + 2

    # ── group into rows ───────────────────────────────────────────────────────
    rows: list[list[dict]] = []
    row_has_form_child: list[bool] = []

    cur_cells: list[dict] = []
    cur_has_form = False
    in_row = False
    # Per-cell link URLs: for cells whose only action is a link, store the href.
    cur_cell_links: list[str] = []
    rows_cell_links: list[list[str]] = []

    for node in tree[table_idx + 1:]:
        d = node["depth"]
        if d <= table_depth:
            break
        if d == row_depth and node["role"] == "row":
            if in_row:
                rows.append(cur_cells)
                row_has_form_child.append(cur_has_form)
                rows_cell_links.append(cur_cell_links)
            cur_cells = []
            cur_cell_links = []
            cur_has_form = False
            in_row = True
        elif d == cell_depth and node["role"] in {"cell", "columnheader", "rowheader"}:
            cur_cells.append(node)
            cur_cell_links.append("")  # placeholder; filled below when link child seen
        elif d > cell_depth and node["role"] in _FILTER_ROLES:
            cur_has_form = True
        elif d > cell_depth and node["role"] == "link" and node.get("url"):
            # Link child inside a cell — store its URL in the last cell's slot.
            if cur_cell_links:
                cur_cell_links[-1] = node["url"]

    if in_row:
        rows.append(cur_cells)
        row_has_form_child.append(cur_has_form)
        rows_cell_links.append(cur_cell_links)

    if not rows:
        return None

    # ── header row ────────────────────────────────────────────────────────────
    header_row_idx = next(
        (i for i, cells in enumerate(rows) if any(c["role"] == "columnheader" for c in cells)),
        None,
    )
    if header_row_idx is None:
        return None

    headers = [c["key"] for c in rows[header_row_idx]]
    norm_headers = [_normalize(h) for h in headers]

    # ── match returns → column indices ────────────────────────────────────────
    field_to_col: dict[str, int] = {}
    for field in returns:
        nf = _normalize(field)
        best_col = -1
        best_score = 0.0
        for col_i, nh in enumerate(norm_headers):
            if not nh:
                continue
            if nh == nf:
                best_col = col_i
                best_score = 1.0
                break
            score = SequenceMatcher(None, nf, nh).ratio()
            if score >= 0.75 and score > best_score:
                best_col = col_i
                best_score = score
        if best_col >= 0:
            field_to_col[field] = best_col

    if not field_to_col:
        return None

    # ── extract data rows ─────────────────────────────────────────────────────
    result_rows: list[dict[str, str]] = []
    _url_suffix = _re.compile(r"_?url$", _re.IGNORECASE)
    for i, (cells, has_form, cell_links) in enumerate(
        zip(rows, row_has_form_child, rows_cell_links)
    ):
        if i == header_row_idx or has_form:
            continue
        row_dict: dict[str, str] = {}
        for field, col_i in field_to_col.items():
            if col_i >= len(cells):
                row_dict[field] = ""
                continue
            # For _url fields prefer the link href from a link child over the cell text.
            if _url_suffix.search(field) and col_i < len(cell_links) and cell_links[col_i]:
                row_dict[field] = cell_links[col_i]
            else:
                row_dict[field] = cells[col_i]["key"].replace("\xa0", " ").strip()
        result_rows.append(row_dict)

    return result_rows


def find_prev_page_ref(tree: list[dict]) -> int | None:
    """Return the ``backendDOMNodeId`` of the 'previous page' button, or None.

    Mirrors ``find_next_page_ref``; used to rewind a grid that loaded mid-pagination
    (e.g. a saved UI-grid bookmark restoring a non-1 ``currentPage``) back to page 1
    before forward collection starts — otherwise rows before the landing page are
    silently lost (only the landing page onward gets collected).
    """
    import re

    _PREV_RE = re.compile(
        "^(prev(ious)?|\u2039|\u2190|<|\ue629)$|prev.?page",
        re.IGNORECASE,
    )
    for node in tree:
        if node["role"] == "button" and node.get("ref"):
            if _PREV_RE.search(node["key"].strip()):
                return node["ref"]
    return None


def find_next_page_ref(tree: list[dict]) -> int | None:
    """Return the ``backendDOMNodeId`` of the 'next page' button in the tree, or None."""
    import re

    _NEXT_RE = re.compile(
        r"^(next|›|→|>||)$|next.?page",
        re.IGNORECASE,
    )
    for node in tree:
        if node["role"] == "button" and node.get("ref"):
            if _NEXT_RE.search(node["key"].strip()):
                return node["ref"]
    return None


def read_from_tree(
    tree: list[dict],
    returns: list[str],
    read_spec: str = "",  # noqa: ARG001 — reserved for future LLM tier
) -> dict[str, str] | None:
    """Extract ``returns`` field values from a semantic tree (scroll-free, exact).

    Returns a ``{field: value}`` dict when ALL fields are resolved,
    or ``None`` when one or more fields cannot be matched (caller should fall
    back to a visual read).  Empty string is a valid resolved value (an empty
    form control IS a result; "not found" is different from "found and empty").

    Matching is two-tier:
    1. Exact normalized-key match (case-insensitive, strip punctuation / sort arrow).
    2. Best-ratio ``difflib.SequenceMatcher`` match above 0.75.

    For form-control roles the node's ``value`` is returned; for data cells /
    headings the node's ``key`` is the content.
    """
    from difflib import SequenceMatcher

    if not tree or not returns:
        return None

    norm_returns = {f: _normalize(f) for f in returns}
    result: dict[str, str] = {}
    unresolved: list[str] = []

    for field in returns:
        nf = norm_returns[field]
        best_node: dict | None = None
        best_score = 0.0
        best_is_form = False

        for node in tree:
            nk = _normalize(node["key"])
            if not nk:
                continue
            is_form = node["role"] in _FORM_ROLES
            if nk == nf:
                # Exact match — prefer form controls over structural nodes.
                if best_score < 1.0 or (not best_is_form and is_form):
                    best_node = node
                    best_score = 1.0
                    best_is_form = is_form
                    if is_form:
                        break  # can't do better
            else:
                score = SequenceMatcher(None, nf, nk).ratio()
                if score >= 0.75 and score > best_score:
                    best_node = node
                    best_score = score
                    best_is_form = is_form

        if best_node is None:
            unresolved.append(field)
        else:
            if best_is_form:
                result[field] = best_node["value"]
            else:
                # Cells / headings: the accessible name IS the content.
                result[field] = best_node["key"]

    if unresolved:
        return None

    return result


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

    return out
