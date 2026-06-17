"""Read-only DOM table snapshots for browser perception.

This module is intentionally a sensor, not an action helper. It extracts the
table/grid structure already present in the current page DOM so downstream read
steps can reason over rows and columns instead of OCR-ing a viewport.
"""

from __future__ import annotations

from typing import Any

MAX_TABLES = 12
MAX_ROWS_PER_TABLE = 500
MAX_CELLS_PER_ROW = 80


def table_snapshot_js() -> str:
    """Return a self-contained JS expression that serializes page tables as JSON."""
    return f"""(() => {{
  const MAX_TABLES = {MAX_TABLES};
  const MAX_ROWS = {MAX_ROWS_PER_TABLE};
  const MAX_CELLS = {MAX_CELLS_PER_ROW};
  const norm = (v) => String(v == null ? "" : v).replace(/\\s+/g, " ").trim();
  const text = (el) => norm(el && (el.innerText || el.value || el.textContent || ""));
  const visible = (el) => {{
    if (!el || !el.isConnected) return false;
    const style = getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden") return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }};
  const cellsOf = (row, selector) => Array.from(row.querySelectorAll(selector))
    .filter(visible).slice(0, MAX_CELLS).map(text);
  const uniquePath = (el) => {{
    const parts = [];
    let n = el;
    while (n && n.nodeType === 1 && parts.length < 4) {{
      let p = n.tagName.toLowerCase();
      if (n.id) p += "#" + n.id;
      else if (n.className && typeof n.className === "string") {{
        const cls = n.className.trim().split(/\\s+/).slice(0, 2).join(".");
        if (cls) p += "." + cls;
      }}
      parts.unshift(p);
      n = n.parentElement;
    }}
    return parts.join(">");
  }};
  const nearbyTitle = (el) => {{
    const label = norm(el.getAttribute("aria-label") || el.getAttribute("data-title") || "");
    if (label) return label;
    const caption = el.querySelector("caption");
    if (caption && text(caption)) return text(caption);
    let prev = el.previousElementSibling;
    for (let i = 0; prev && i < 4; i++, prev = prev.previousElementSibling) {{
      if (/^H[1-6]$/.test(prev.tagName) && text(prev)) return text(prev);
    }}
    let parent = el.parentElement;
    for (let depth = 0; parent && depth < 4; depth++, parent = parent.parentElement) {{
      const heading = parent.querySelector("h1,h2,h3,h4,h5,h6,.admin__page-section-title,.page-title");
      if (heading && text(heading)) return text(heading);
    }}
    return "";
  }};
  const totalRecordsNear = (el) => {{
    let node = el;
    for (let depth = 0; node && depth < 5; depth++, node = node.parentElement) {{
      const s = text(node).slice(0, 8000);
      let m = s.match(/([0-9][0-9,]*)\\s+(?:records?|items?)\\s+found/i);
      if (m) return Number(m[1].replace(/,/g, ""));
      m = s.match(/(?:of|\\/|共)\\s*([0-9][0-9,]*)\\s+(?:records?|items?|条)?/i);
      if (m) return Number(m[1].replace(/,/g, ""));
    }}
    return null;
  }};
  const finalize = (entry) => {{
    entry.headers = (entry.headers || []).map(norm);
    entry.rows = (entry.rows || []).map((r) => r.map(norm)).filter((r) => r.some(Boolean));
    entry.domRows = entry.rows.length;
    entry.rows = entry.rows.slice(0, MAX_ROWS);
    entry.totalRecords = entry.totalRecords || null;
    entry.partial = !!(entry.totalRecords && entry.rows.length < entry.totalRecords);
    entry.path = entry.path || "";
    return entry;
  }};
  const snapshots = [];
  const seen = new Set();

  for (const table of Array.from(document.querySelectorAll("table"))) {{
    if (snapshots.length >= MAX_TABLES) break;
    if (!visible(table) || (table.parentElement && table.parentElement.closest("table"))) continue;
    const allRows = Array.from(table.querySelectorAll("tr")).filter(visible);
    if (!allRows.length) continue;
    let headerRow = Array.from(table.querySelectorAll("thead tr")).find((r) => cellsOf(r, "th,td").some(Boolean));
    if (!headerRow) headerRow = allRows.find((r) => Array.from(r.querySelectorAll("th")).some(visible));
    const headerCells = headerRow ? cellsOf(headerRow, "th,td") : [];
    const bodyRows = Array.from(table.querySelectorAll("tbody tr")).filter(visible);
    const dataRows = (bodyRows.length ? bodyRows : allRows.filter((r) => r !== headerRow));
    const rows = dataRows.map((r) => cellsOf(r, "th,td")).filter((r) => r.length > 0);
    if (!headerCells.length && rows.length < 2) continue;
    if (Math.max(headerCells.length, ...(rows.map((r) => r.length))) < 2) continue;
    snapshots.push(finalize({{
      source: "table",
      caption: nearbyTitle(table),
      headers: headerCells,
      rows,
      totalRecords: totalRecordsNear(table),
      path: uniquePath(table),
    }}));
    seen.add(table);
  }}

  const gridSelectors = '[role="grid"],[role="treegrid"],.ag-root,.MuiDataGrid-root';
  for (const grid of Array.from(document.querySelectorAll(gridSelectors))) {{
    if (snapshots.length >= MAX_TABLES) break;
    if (!visible(grid) || grid.querySelector("table")) continue;
    if (Array.from(seen).some((t) => grid.contains(t))) continue;
    const headers = Array.from(grid.querySelectorAll('[role="columnheader"]'))
      .filter(visible).slice(0, MAX_CELLS).map(text);
    const rows = Array.from(grid.querySelectorAll('[role="row"]'))
      .filter(visible)
      .filter((r) => !r.querySelector('[role="columnheader"]'))
      .map((r) => cellsOf(r, '[role="gridcell"],[role="cell"],[role="rowheader"],td,th'))
      .filter((r) => r.length > 0);
    if (!headers.length && rows.length < 2) continue;
    if (Math.max(headers.length, ...(rows.map((r) => r.length))) < 2) continue;
    snapshots.push(finalize({{
      source: "aria-grid",
      caption: nearbyTitle(grid),
      headers,
      rows,
      totalRecords: totalRecordsNear(grid),
      path: uniquePath(grid),
    }}));
  }}

  return JSON.stringify({{
    url: location.href,
    title: document.title,
    tables: snapshots,
  }});
}})()"""


def normalize_table_snapshots(raw: Any) -> list[dict[str, Any]]:
    """Normalize raw JS table snapshots into row dictionaries."""
    if not isinstance(raw, dict):
        return []
    page = {
        "url": str(raw.get("url") or ""),
        "title": str(raw.get("title") or ""),
    }
    out: list[dict[str, Any]] = []
    for idx, item in enumerate(raw.get("tables") or [], start=1):
        if not isinstance(item, dict):
            continue
        raw_rows = item.get("rows") if isinstance(item.get("rows"), list) else []
        width = max(
            len(item.get("headers") or []),
            *(len(r) for r in raw_rows if isinstance(r, list)),
            0,
        )
        if width < 2:
            continue
        headers = _dedupe_headers(item.get("headers") or [], width)
        rows: list[dict[str, str]] = []
        for raw_row in raw_rows:
            if not isinstance(raw_row, list):
                continue
            values = [str(v or "").strip() for v in raw_row[:width]]
            if not any(values):
                continue
            rows.append({headers[i]: (values[i] if i < len(values) else "") for i in range(width)})
        if not rows:
            continue
        total_records = _safe_int(item.get("totalRecords"))
        out.append(
            {
                "index": idx,
                "source": str(item.get("source") or "table"),
                "caption": str(item.get("caption") or ""),
                "headers": headers,
                "rows": rows,
                "row_count": len(rows),
                "dom_row_count": _safe_int(item.get("domRows")) or len(rows),
                "total_records": total_records,
                "partial": bool(item.get("partial") or (total_records and len(rows) < total_records)),
                "path": str(item.get("path") or ""),
                "page": page,
            }
        )
    return out


def _dedupe_headers(headers: list[Any], width: int) -> list[str]:
    names = [str(h or "").strip() for h in headers[:width]]
    while len(names) < width:
        names.append("")
    seen: dict[str, int] = {}
    out: list[str] = []
    for i, name in enumerate(names, start=1):
        base = name or f"col_{i}"
        count = seen.get(base, 0) + 1
        seen[base] = count
        out.append(base if count == 1 else f"{base}_{count}")
    return out


def _safe_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        n = int(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None
