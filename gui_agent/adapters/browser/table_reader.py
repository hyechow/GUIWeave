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
MAX_COMPLETE_ROWS_PER_TABLE = 5000
COMPLETE_PAGE_SIZE = 500


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
  const titleSelectors = [
    ".dashboard-item-title",
    ".admin__page-section-title",
    ".page-title",
    ".panel-title",
    ".box-title",
    ".block-title",
    ".title",
    "[data-role='title']",
    "[role='heading']",
    "legend",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
  ].join(",");
  const labelledTitle = (el) => {{
    const id = norm(el && el.getAttribute && el.getAttribute("aria-labelledby"));
    if (!id) return "";
    const labelled = document.getElementById(id);
    return labelled && visible(labelled) ? text(labelled) : "";
  }};
  const titleFromContainer = (container, target) => {{
    if (!container) return "";
    const explicit = norm(container.getAttribute("aria-label") || container.getAttribute("data-title") || "");
    if (explicit) return explicit;
    const labelled = labelledTitle(container);
    if (labelled) return labelled;
    const candidates = Array.from(container.querySelectorAll(titleSelectors)).filter((cand) => (
      visible(cand) && cand !== target && !target.contains(cand) && !cand.contains(target)
    ));
    for (const cand of candidates) {{
      if (cand.compareDocumentPosition(target) & Node.DOCUMENT_POSITION_FOLLOWING) {{
        const s = text(cand);
        if (s) return s;
      }}
    }}
    return "";
  }};
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
    const labelled = labelledTitle(el);
    if (labelled) return labelled;
    const caption = el.querySelector("caption");
    if (caption && text(caption)) return text(caption);
    let prev = el.previousElementSibling;
    for (let i = 0; prev && i < 4; i++, prev = prev.previousElementSibling) {{
      if (prev.matches && prev.matches(titleSelectors) && text(prev)) return text(prev);
    }}
    let parent = el.parentElement;
    for (let depth = 0; parent && depth < 6; depth++, parent = parent.parentElement) {{
      const local = titleFromContainer(parent, el);
      if (local) return local;
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

  const detectPagerState = (tableOrGrid) => {{
    let container = tableOrGrid.parentElement;
    for (let depth = 0; depth < 4 && container; depth++, container = container.parentElement) {{
      const pagerSelectors = [
        '.pager', '.pagination', '[role="navigation"][aria-label*="page" i]',
        '.pages', '.page-numbers', '.data-grid-paginator', '.admin__data-grid-pager',
        'nav[aria-label*="pagination" i]', '[aria-label*="page" i]',
      ].join(',');
      const pager = Array.from(container.querySelectorAll(pagerSelectors)).find(p => {{
        // Direct containment (one inside the other)
        if (tableOrGrid.contains(p) || p.contains(tableOrGrid)) return true;
        // Check if table/pager (or their ancestors) are siblings under current container
        const findChildInContainer = (el) => {{
          let current = el;
          while (current && current !== container) {{
            if (current.parentElement === container) return current;
            current = current.parentElement;
          }}
          return null;
        }};
        const tableChild = findChildInContainer(tableOrGrid);
        const pagerChild = findChildInContainer(p);
        if (tableChild && pagerChild) {{
          const tableIdx = Array.from(container.children).indexOf(tableChild);
          const pagerIdx = Array.from(container.children).indexOf(pagerChild);
          if (tableIdx >= 0 && pagerIdx >= 0 && Math.abs(tableIdx - pagerIdx) <= 6) return true;
        }}
        return false;
      }});
      if (pager) return readPagedPager(pager);
    }}
    return detectScrollState(tableOrGrid);
  }};

  const readPagedPager = (pager) => {{
    let page_index = null;
    let page_count = null;
    let has_next_page = null;
    let has_prev_page = null;

    const text = pager.innerText || '';
    const pageMatch = text.match(/(?:page\\s*)?(\\d+)\\s*(?:of|\\/|共)\\s*(?:page\\s*)?(\\d+)/i)
      || text.match(/(\\d+)\\s*-\\s*\\d+\\s*(?:of|\\/|共)\\s*(\\d+)/i);
    if (pageMatch) {{
      page_index = parseInt(pageMatch[1]);
      page_count = parseInt(pageMatch[2]);
    }}

    // Magento-style: <input id="*_page-current" value="N"> + <label>of <span>M</span></label>
    if (!page_index) {{
      const pageInput = pager.querySelector('input[id*="page-current" i], input[name="page" i]');
      if (pageInput) {{
        const val = parseInt(pageInput.value);
        if (val) page_index = val;
      }}
    }}
    if (!page_count) {{
      const label = pager.querySelector('label[for*="page-current" i], label.admin__control-support-text');
      if (label) {{
        const span = label.querySelector('span');
        const totalText = span ? span.innerText : label.innerText;
        const match = totalText.match(/\\d+/);
        if (match) page_count = parseInt(match[0]);
      }}
    }}

    const nextBtn = pager.querySelector([
      '[aria-label*="next page" i]',
      '[aria-label*="下一页" i]',
      '[role="button"][aria-label*="next" i]',
      '.next-page', '.action-next',
      'button[title*="Next" i]',
      'button[class*="next" i]',
    ].join(', '));
    const prevBtn = pager.querySelector([
      '[aria-label*="previous page" i]',
      '[aria-label*="上一页" i]',
      '[role="button"][aria-label*="previous" i]',
      '.prev-page', '.action-previous',
      'button[title*="Previous" i]',
      'button[class*="previous" i]',
    ].join(', '));

    if (nextBtn) {{
      const disabled = nextBtn.disabled || nextBtn.matches('[disabled], [aria-disabled="true"], .disabled, [class*="disabled" i]');
      has_next_page = !disabled;
    }}
    if (prevBtn) {{
      const disabled = prevBtn.disabled || prevBtn.matches('[disabled], [aria-disabled="true"], .disabled, [class*="disabled" i]');
      has_prev_page = !disabled;
    }}

    if (!page_index) {{
      const activePage = pager.querySelector([
        '.active[role="button"]',
        '[aria-current="page"]',
        '.current',
        '[aria-selected="true"]',
      ].join(', '));
      if (activePage) {{
        const pageNum = parseInt(activePage.innerText || activePage.getAttribute('data-page'));
        if (pageNum) page_index = pageNum;
      }}
    }}

    return {{
      type: 'paged',
      page_index,
      page_count,
      has_next_page,
      has_prev_page,
    }};
  }};

  const detectScrollState = (el) => {{
    let container = el.parentElement;
    for (let depth = 0; depth < 4 && container; depth++, container = container.parentElement) {{
      const style = getComputedStyle(container);
      const overflow = style.overflow || style.overflowY || '';
      if (['auto', 'scroll', 'overlay'].includes(overflow) && container.scrollHeight > 0) {{
        const can_scroll_more = container.scrollHeight > container.scrollTop + container.clientHeight + 2;
        return {{
          type: 'scroll',
          can_scroll_more,
          at_scroll_end: !can_scroll_more,
        }};
      }}
    }}
    return {{ type: 'unknown' }};
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
    const traversal = detectPagerState(table);
    snapshots.push(finalize({{
      source: "table",
      caption: nearbyTitle(table),
      headers: headerCells,
      rows,
      totalRecords: totalRecordsNear(table),
      path: uniquePath(table),
      traversal,
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
    if (Math.max(headers.length, ...rows.map((r) => r.length)) < 2) continue;
    const traversal = detectPagerState(grid);
    snapshots.push(finalize({{
      source: "aria-grid",
      caption: nearbyTitle(grid),
      headers,
      rows,
      totalRecords: totalRecordsNear(grid),
      path: uniquePath(grid),
      traversal,
    }}));
  }}

  return JSON.stringify({{
    url: location.href,
    title: document.title,
    tables: snapshots,
  }});
}})()"""


def complete_table_snapshot_js() -> str:
    """Return JS that fetches full read-only Magento MUI grid pages when available.

    The normal DOM reader only sees rows mounted in the current grid. Magento Admin
    grids also expose a same-origin JSON provider (`/mui/index/render/`) with
    `items` and `totalRecords`. Running this inside the authenticated page lets a
    data_query consume the complete current grid dataset without UI scrolling.
    """
    return f"""(async () => {{
  const MAX_TABLES = {MAX_TABLES};
  const MAX_ROWS = {MAX_COMPLETE_ROWS_PER_TABLE};
  const PAGE_SIZE = {COMPLETE_PAGE_SIZE};
  const norm = (v) => String(v == null ? "" : v).replace(/\\s+/g, " ").trim();
  const scalar = (v) => {{
    if (v == null) return "";
    if (typeof v === "string" || typeof v === "number" || typeof v === "boolean") return norm(v);
    if (Array.isArray(v)) return v.map((x) => norm(x)).filter(Boolean).join(", ");
    return "";
  }};
  const rowFromItem = (item) => {{
    const row = {{}};
    if (!item || typeof item !== "object" || Array.isArray(item)) return row;
    for (const [key, val] of Object.entries(item)) {{
      const out = scalar(val);
      if (out || typeof val === "string" || typeof val === "number" || typeof val === "boolean") {{
        row[key] = out;
      }}
    }}
    return row;
  }};
  const candidateUrls = performance.getEntriesByType("resource")
    .map((e) => e.name)
    .filter((url) => url.includes("/mui/index/render/") && url.includes("namespace="));
  const latestByNamespace = new Map();
  for (const raw of candidateUrls) {{
    try {{
      const url = new URL(raw, location.href);
      const namespace = url.searchParams.get("namespace") || "";
      if (!namespace || namespace === "notification_area") continue;
      latestByNamespace.delete(namespace);
      latestByNamespace.set(namespace, url.toString());
    }} catch (_) {{}}
  }}
  const fetchPage = async (baseUrl, page) => {{
    const url = new URL(baseUrl, location.href);
    url.searchParams.set("paging[pageSize]", String(PAGE_SIZE));
    url.searchParams.set("paging[current]", String(page));
    url.searchParams.set("isAjax", "true");
    const res = await fetch(url.toString(), {{
      credentials: "same-origin",
      headers: {{
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
      }},
    }});
    if (!res.ok) return null;
    const data = await res.json();
    if (!data || data.ajaxExpired || !Array.isArray(data.items)) return null;
    return data;
  }};
  const tables = [];
  for (const [namespace, url] of Array.from(latestByNamespace).slice(-MAX_TABLES)) {{
    const first = await fetchPage(url, 1);
    if (!first) continue;
    const total = Number(first.totalRecords || first.items.length || 0);
    const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
    const items = [...first.items];
    for (let page = 2; page <= pages && items.length < MAX_ROWS; page++) {{
      const next = await fetchPage(url, page);
      if (!next) break;
      items.push(...next.items);
    }}
    const rows = items.slice(0, MAX_ROWS).map(rowFromItem).filter((row) => Object.keys(row).length);
    if (!rows.length) continue;
    const headers = [];
    const seen = new Set();
    for (const row of rows) {{
      for (const key of Object.keys(row)) {{
        if (!seen.has(key)) {{
          seen.add(key);
          headers.push(key);
        }}
      }}
    }}
    if (headers.length < 2) continue;
    tables.push({{
      source: "magento-mui",
      caption: namespace,
      headers,
      rows,
      domRows: rows.length,
      totalRecords: total || rows.length,
      partial: !!(total && rows.length < total),
      path: "/mui/index/render/?namespace=" + namespace,
    }});
  }}
  return JSON.stringify({{
    url: location.href,
    title: document.title,
    tables,
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
        dict_headers: list[str] = []
        for raw_row in raw_rows:
            if isinstance(raw_row, dict):
                for key in raw_row:
                    key_s = str(key or "").strip()
                    if key_s and key_s not in dict_headers:
                        dict_headers.append(key_s)
        raw_headers = item.get("headers") or dict_headers
        width = max(
            len(raw_headers),
            *(len(r) for r in raw_rows if isinstance(r, list)),
            0,
        )
        if width < 2:
            continue
        headers = _dedupe_headers(raw_headers, width)
        rows: list[dict[str, str]] = []
        for raw_row in raw_rows:
            if isinstance(raw_row, dict):
                row = {header: str(raw_row.get(header, "") or "").strip() for header in headers}
                if any(row.values()):
                    rows.append(row)
                continue
            if not isinstance(raw_row, list):
                continue
            values = [str(v or "").strip() for v in raw_row[:width]]
            if not any(values):
                continue
            rows.append({headers[i]: (values[i] if i < len(values) else "") for i in range(width)})
        if not rows:
            continue
        total_records = _safe_int(item.get("totalRecords"))
        traversal = item.get("traversal")  # 新增: pager/scroll traversal state
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
                "traversal": traversal if isinstance(traversal, dict) else None,  # 新增
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
