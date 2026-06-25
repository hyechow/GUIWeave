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

  const parsePositiveInt = (value) => {{
    const m = String(value == null ? "" : value).replace(/,/g, "").match(/\\d+/);
    if (!m) return null;
    const n = parseInt(m[0]);
    return Number.isFinite(n) && n > 0 ? n : null;
  }};
  const labelTextFor = (control, root) => {{
    const parts = [];
    const id = control && control.id;
    if (id) {{
      const label = (root || document).querySelector(`label[for="${{CSS.escape(id)}}"]`);
      if (label) parts.push(label.textContent || label.innerText || "");
    }}
    const labelledBy = control && control.getAttribute && control.getAttribute("aria-labelledby");
    if (labelledBy) {{
      for (const ref of labelledBy.split(/\\s+/)) {{
        const label = document.getElementById(ref);
        if (label) parts.push(label.textContent || label.innerText || "");
      }}
    }}
    return norm(parts.join(" "));
  }};
  const disabled = (el) => !!(
    el && (
      el.disabled ||
      el.matches('[disabled], [aria-disabled="true"], .disabled, [class*="disabled" i]') ||
      el.closest('[disabled], [aria-disabled="true"], .disabled, [class*="disabled" i]')
    )
  );
  const detectPageSizeState = (pager) => {{
    let root = pager;
    for (let depth = 0; root && depth < 5; depth++, root = root.parentElement) {{
      const candidates = Array.from(root.querySelectorAll([
        'select',
        'input',
        '[role="combobox"]',
        '.selectmenu input',
        '[class*="page-size" i] input',
        '[class*="per-page" i] input',
      ].join(',')));
      for (const control of candidates) {{
        const label = labelTextFor(control, root);
        let menu = control.closest('.selectmenu, [class*="page-size" i], [class*="per-page" i]');
        const nearby = norm((menu || control.parentElement || control).textContent || "");
        const attrs = norm([
          control.getAttribute("name"),
          control.getAttribute("id"),
          control.getAttribute("aria-label"),
          control.getAttribute("aria-labelledby"),
          label,
        ].join(" "));
        if (
          !/(per page|page size|rows per page|items per page|每页|每頁)/i.test(attrs) &&
          !(menu && /(per page|page size|rows per page|items per page|每页|每頁)/i.test(nearby))
        ) continue;

        const current = parsePositiveInt(control.value || control.getAttribute("value") || control.textContent);
        menu = menu || root;
        const optionNodes = Array.from(menu.querySelectorAll([
          'option',
          '[role="option"]',
          '.selectmenu-item',
          '.selectmenu-items li',
          '.selectmenu-items button',
          '.selectmenu-items a',
          'button',
          'a',
        ].join(',')));
        const options = [];
        const seen = new Set();
        for (const option of optionNodes) {{
          const n = parsePositiveInt(option.textContent || option.innerText || option.value);
          if (!n || seen.has(n)) continue;
          seen.add(n);
          options.push(n);
        }}
        options.sort((a, b) => a - b);
        const activeMenu = menu.querySelector('.selectmenu-items._active, [role="listbox"], ._active');
        const page_size_menu_open = !!(activeMenu && visible(activeMenu));
        return {{
          page_size: current,
          page_size_options: options,
          has_page_size_control: true,
          page_size_menu_open,
        }};
      }}
    }}
    return {{
      page_size: null,
      page_size_options: [],
      has_page_size_control: false,
      page_size_menu_open: false,
    }};
  }};

  const PAGER_SELECTORS = [
    '.pager', '.pagination', '[role="navigation"][aria-label*="page" i]',
    '.pages', '.page-numbers', '.data-grid-paginator', '.admin__data-grid-pager',
    'nav[aria-label*="pagination" i]', '[aria-label*="page" i]',
  ].join(',');

  const detectPagerState = (tableOrGrid) => {{
    let container = tableOrGrid.parentElement;
    for (let depth = 0; depth < 4 && container; depth++, container = container.parentElement) {{
      const pager = Array.from(container.querySelectorAll(PAGER_SELECTORS)).find(p => {{
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

    // Magento-style: <input data-ui-id="current-page-input" value="N"> + <label>of M</label>.
    // The input id is often a dynamic number, so prefer the label's `for` link over id patterns.
    const pageLabels = Array.from(pager.querySelectorAll('label.admin__control-support-text, label[for]'));
    for (const label of pageLabels) {{
      const labelText = label.innerText || '';
      if (!/\\bof\\s+\\d+/i.test(labelText)) continue;
      const forId = label.getAttribute('for');
      const linkedInput = forId ? pager.querySelector(`#${{CSS.escape(forId)}}`) : null;
      if (!page_index && linkedInput) {{
        const val = parseInt(linkedInput.value);
        if (val) page_index = val;
      }}
      if (!page_count) {{
        const match = labelText.match(/\\d+/);
        if (match) page_count = parseInt(match[0]);
      }}
    }}

    // Other Magento variants: <input id="*_page-current" value="N">.
    if (!page_index) {{
      const pageInput = pager.querySelector('input[data-ui-id="current-page-input" i], input[id*="page-current" i], input[name="page" i]');
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

    if (nextBtn) has_next_page = !disabled(nextBtn);
    if (prevBtn) has_prev_page = !disabled(prevBtn);

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

    const pageSize = detectPageSizeState(pager);
    return {{
      type: 'paged',
      page_index,
      page_count,
      has_next_page,
      has_prev_page,
      ...pageSize,
    }};
  }};

  const scrollStateOf = (container) => {{
    const can_scroll_more = container.scrollHeight > container.scrollTop + container.clientHeight + 2;
    return {{
      type: 'scroll',
      can_scroll_more,
      at_scroll_end: !can_scroll_more,
    }};
  }};

  const detectScrollState = (el) => {{
    let container = el.parentElement;
    for (let depth = 0; depth < 4 && container; depth++, container = container.parentElement) {{
      const style = getComputedStyle(container);
      const overflow = style.overflow || style.overflowY || '';
      if (['auto', 'scroll', 'overlay'].includes(overflow) && container.scrollHeight > 0) {{
        return scrollStateOf(container);
      }}
    }}
    return {{ type: 'unknown' }};
  }};

  // Page-level traversal sensor: NOT anchored to a table/grid. Used as the canonical
  // ``viewport`` signal so card/feed-style collections (no <table>) still get a
  // deterministic pagination/scroll-boundary signal instead of falling back to the LLM.
  const detectPageViewport = (tableSnapshots) => {{
    for (const snap of tableSnapshots) {{
      if (snap.traversal && snap.traversal.type !== 'unknown') return snap.traversal;
    }}
    // PAGER_SELECTORS' catch-all `[aria-label*="page" i]` can match a single page-NUMBER link
    // (e.g. Google's <a aria-label="Page 2">) instead of the pagination bar that wraps it —
    // readPagedPager() on that tiny element finds no index/buttons, returning an uninformative
    // 'paged' result that would otherwise read as a confident "no next page -> done". Only trust
    // a page-level (non-table-anchored) pager match when it actually resolved something.
    const pager = Array.from(document.querySelectorAll(PAGER_SELECTORS)).find(visible);
    if (pager) {{
      const state = readPagedPager(pager);
      if (state.page_index != null || state.has_next_page != null || state.has_prev_page != null) {{
        return state;
      }}
    }}
    // Whole-document scroll (the common case): most ordinary pages scroll via the browser
    // viewport itself with <html>/<body> at the default `overflow: visible` — which the
    // overflow:auto/scroll/overlay scan below never matches, even though the page plainly
    // scrolls. Check this BEFORE the inner-container scan so a normal page (e.g. a search
    // results list with no wrapping <div overflow:auto>) gets a real signal instead of
    // accidentally matching some unrelated small scrollable widget elsewhere on the page.
    const docHeight = document.documentElement.scrollHeight;
    const docTop = window.scrollY || document.documentElement.scrollTop || document.body.scrollTop;
    const viewH = window.innerHeight;
    if (docHeight > viewH + 2) {{
      const can_scroll_more = docHeight > docTop + viewH + 2;
      return {{ type: 'scroll', can_scroll_more, at_scroll_end: !can_scroll_more }};
    }}
    // SPA shells where the document itself doesn't grow but an inner panel scrolls.
    let best = null;
    let bestArea = 0;
    for (const el of Array.from(document.querySelectorAll('*'))) {{
      if (el === document.documentElement || el === document.body) continue;
      if (!visible(el) || el.scrollHeight <= el.clientHeight) continue;
      const style = getComputedStyle(el);
      const overflow = style.overflow || style.overflowY || '';
      if (!['auto', 'scroll', 'overlay'].includes(overflow)) continue;
      const area = el.clientWidth * el.clientHeight;
      if (area > bestArea) {{
        bestArea = area;
        best = el;
      }}
    }}
    if (best) return scrollStateOf(best);
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
    viewport: detectPageViewport(snapshots),
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
        traversal = item.get("traversal")
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
                # Legacy table-scoped copy of this table's own pager/scroll state. Kept for
                # back-compat and as detectPageViewport()'s reuse source; NOT the traversal
                # decision's authority — that's Observation.viewport (see schemas docstring).
                "traversal": traversal if isinstance(traversal, dict) else None,
            }
        )
    return out


def normalize_viewport(raw: Any) -> dict[str, Any] | None:
    """Extract the page-level traversal/scroll-boundary signal (independent of tables)."""
    if not isinstance(raw, dict):
        return None
    viewport = raw.get("viewport")
    if not isinstance(viewport, dict):
        return None
    if viewport.get("type") in (None, "unknown"):
        return None
    return viewport


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
