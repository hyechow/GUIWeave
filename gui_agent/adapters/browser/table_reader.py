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
  const viewportState = (el) => {{
    const r = el.getBoundingClientRect();
    const vh = innerHeight || document.documentElement.clientHeight;
    const vw = innerWidth || document.documentElement.clientWidth;
    const inViewport = r.bottom >= 0 && r.right >= 0 && r.top <= vh && r.left <= vw;
    let viewportPos = "in";
    if (r.bottom < 0) viewportPos = "above";
    else if (r.top > vh) viewportPos = "below";
    return {{ in_viewport: inViewport, viewport_pos: viewportPos }};
  }};
  const cellsOf = (row, selector) => Array.from(row.querySelectorAll(selector))
    .filter(visible).slice(0, MAX_CELLS).map(text);
  // A cell's href is part of the cell's complete content, not a special "url" field:
  // take the first navigable link inside the cell (absolute URL), "" when there is none.
  const cellLink = (el) => {{
    if (!el) return "";
    const a = el.matches && el.matches("a[href]") ? el : (el.querySelector && el.querySelector("a[href]"));
    if (!a) return "";
    const raw = a.getAttribute("href") || "";
    if (!raw || /^(javascript:|#|mailto:|tel:|data:)/i.test(raw)) return "";
    return a.href || "";  // resolved absolute URL
  }};
  const linksOf = (row, selector) => Array.from(row.querySelectorAll(selector))
    .filter(visible).slice(0, MAX_CELLS).map(cellLink);
  const titleSelectors = [
    "[class*='title' i]",
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
    // Keep rows and their per-cell links aligned through the same empty-row filter + slice.
    const links = entry.rowLinks || [];
    const paired = (entry.rows || [])
      .map((r, i) => ({{ cells: r.map(norm), links: (links[i] || []).slice() }}))
      .filter((p) => p.cells.some(Boolean));
    entry.domRows = paired.length;
    const kept = paired.slice(0, MAX_ROWS);
    entry.rows = kept.map((p) => p.cells);
    entry.rowLinks = kept.map((p) => p.links);
    entry.totalRecords = entry.totalRecords ?? null;
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
        '[class*="selectmenu" i] input',
        '[class*="page-size" i] input',
        '[class*="per-page" i] input',
      ].join(',')));
      for (const control of candidates) {{
        const label = labelTextFor(control, root);
        // The widget root is the outermost CONTIGUOUS matching ancestor: inner wrappers (a value
        // box around the display input) match the same tokens but hold neither label nor options.
        let menu = null;
        for (let n = control.parentElement, d = 0; n && d < 5; d += 1, n = n.parentElement) {{
          if (n.matches && n.matches('[class*="selectmenu" i], [class*="page-size" i], [class*="per-page" i]')) {{ menu = n; }}
          else if (menu) break;
        }}
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
          '[class*="selectmenu" i] li',
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
        const rect = control.getBoundingClientRect();
        return {{
          page_size: current,
          page_size_options: options,
          page_size_control: {{
            kind: control.tagName === 'SELECT' ? 'native_select' : 'selectmenu',
            x: rect.left + rect.width / 2,
            y: rect.top + rect.height / 2,
          }},
        }};
      }}
    }}
    return {{
      page_size: null,
      page_size_options: [],
    }};
  }};

  const PAGER_SELECTORS = [
    '[class*="pager" i]', '[class*="paginat" i]', '.pages', '.page-numbers',
    '[role="navigation"][aria-label*="page" i]',
    'nav[aria-label*="pagination" i]', '[aria-label*="page" i]',
  ].join(',');

  const pagerControlLabel = (el) => norm([
    el.getAttribute('aria-label'), el.getAttribute('title'),
    text(el),
  ].filter(Boolean).join(' '));
  const pagerControl = (root, direction) => {{
    const next = direction === 'next';
    const name = next ? 'next' : 'previous';
    const selector = [
      `[aria-label*="${{name}} page" i]`, `[aria-label*="${{next ? '下一页' : '上一页'}}" i]`,
      `[role="button"][aria-label*="${{name}}" i]`, `.${{next ? 'next' : 'prev'}}-page`,
      `.action-${{name}}`, `button[title*="${{name}}" i]`, `button[class*="${{name}}" i]`,
    ].join(', ');
    if (root.matches && root.matches(selector)) return root;
    const selected = root.querySelector(selector);
    if (selected) return selected;
    const pattern = next
      ? /^(?:next(?:\\s+page)?|下一页|下一頁)(?:\\s|$|[→›»])/i
      : /^(?:previous(?:\\s+page)?|prev|上一页|上一頁)(?:\\s|$|[←‹«])/i;
    return Array.from(root.querySelectorAll('button, a, [role="button"]'))
      .find(el => visible(el) && pattern.test(pagerControlLabel(el))) || null;
  }};
  const pagerFromControls = (container, tableOrGrid) => {{
    const next = pagerControl(container, 'next');
    const prev = pagerControl(container, 'previous');
    const anchor = next || prev;
    if (!anchor) return null;
    const tableRect = tableOrGrid.getBoundingClientRect();
    const anchorRect = anchor.getBoundingClientRect();
    if (anchorRect.bottom < tableRect.top || anchorRect.top > tableRect.bottom + 320) return null;
    let root = anchor.parentElement;
    for (let depth = 0; root && root !== container.parentElement && depth < 5; depth++, root = root.parentElement) {{
      if ((!next || root.contains(next)) && (!prev || root.contains(prev))) return root;
    }}
    return anchor.parentElement;
  }};

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
      if (pager) {{
        const state = readPagedPager(pager);
        if (state.page_index != null || state.page_count != null ||
            state.has_next_page != null || state.has_prev_page != null) return state;
      }}
      const inferred = pagerFromControls(container, tableOrGrid);
      if (inferred) return readPagedPager(inferred);
    }}
    return detectScrollState(tableOrGrid);
  }};

  const readPagedPager = (pager) => {{
    let page_index = null;
    let page_count = null;
    let has_next_page = null;
    let has_prev_page = null;

    const pagerText = pager.innerText || '';
    const pageMatch = pagerText.match(/(?:page\\s*)?(\\d+)\\s*(?:of|\\/|共)\\s*(?:page\\s*)?(\\d+)/i)
      || pagerText.match(/(\\d+)\\s*-\\s*\\d+\\s*(?:of|\\/|共)\\s*(\\d+)/i);
    if (pageMatch) {{
      page_index = parseInt(pageMatch[1]);
      page_count = parseInt(pageMatch[2]);
    }}

    // Paged-pager pattern: an <input> holding the current page plus a label reading "of M".
    // The input id is often dynamic, so prefer the label's `for` link over id patterns.
    const pageLabels = Array.from(pager.querySelectorAll('label'));
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

    // Other variants: the current-page input identified by generic id/name/label patterns.
    if (!page_index) {{
      const pageInput = pager.querySelector('input[id*="page-current" i], input[name="page" i], input[aria-label*="page" i]');
      if (pageInput) {{
        const val = parseInt(pageInput.value);
        if (val) page_index = val;
      }}
    }}
    if (!page_count) {{
      const label = pager.querySelector('label[for*="page-current" i]');
      if (label) {{
        const span = label.querySelector('span');
        const totalText = span ? span.innerText : label.innerText;
        const match = totalText.match(/\\d+/);
        if (match) page_count = parseInt(match[0]);
      }}
    }}

    const nextBtn = pagerControl(pager, 'next');
    const prevBtn = pagerControl(pager, 'previous');

    if (nextBtn) has_next_page = !disabled(nextBtn);
    if (prevBtn) has_prev_page = !disabled(prevBtn);

    const pageValue = (el) => {{
      const raw = text(el);
      if (/^\\d+$/.test(raw)) return parseInt(raw);
      const active = el.matches('.current, [aria-current="page"]')
        || el.parentElement?.matches('.current, [aria-current="page"]');
      const actionable = el.matches('a, button, [role="button"]');
      if (!active && !actionable) return null;
      const match = raw.match(/(\\d+)\\s*$/);
      return match ? parseInt(match[1]) : null;
    }};
    // Current pages are often non-link elements. Include semantic current-page
    // state so a terminal pager with only Previous still resolves deterministically.
    const pageItems = Array.from(pager.querySelectorAll(
      'button, a, [role="button"], .current, [aria-current="page"]'
    ))
      .map(el => ({{ el, value: pageValue(el) }}))
      .filter(item => item.value != null);
    const pageNumbers = pageItems.map(item => item.value);
    if (!page_count && pageNumbers.length) page_count = Math.max(...pageNumbers);

    if (!page_index) {{
      const activeSelector = '.active, .current, [aria-current="page"], '
        + '[aria-selected="true"], [data-state="active"], [data-active="true"]';
      const activePage = pageItems.find(item =>
        item.el.matches(activeSelector) || item.el.parentElement?.matches(activeSelector)
      );
      if (activePage) page_index = activePage.value;
    }}
    if (!page_index && pageItems.length >= 3) {{
      const styled = pageItems.map(item => {{
        const style = getComputedStyle(item.el);
        return {{ ...item, key: [style.backgroundColor, style.borderTopColor,
          style.borderTopWidth, style.color, style.fontWeight, style.boxShadow].join('|') }};
      }});
      const counts = styled.reduce((all, item) => (all[item.key] = (all[item.key] || 0) + 1, all), {{}});
      const unique = styled.filter(item => counts[item.key] === 1);
      if (unique.length === 1 && Math.max(...Object.values(counts)) >= 2) page_index = unique[0].value;
    }}
    if (!page_index && prevBtn && disabled(prevBtn) && pageNumbers.includes(1)) page_index = 1;
    if (!page_index && nextBtn && disabled(nextBtn) && page_count) page_index = page_count;
    if (has_next_page == null && page_index && page_count) has_next_page = page_index < page_count;
    if (has_prev_page == null && page_index) has_prev_page = page_index > 1;

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
    const scroll_top = Math.max(0, container.scrollTop || 0);
    const can_scroll_more = container.scrollHeight > container.scrollTop + container.clientHeight + 2;
    return {{
      type: 'scroll',
      scroll_top,
      scroll_extent: container.scrollHeight,
      viewport_extent: container.clientHeight,
      at_scroll_start: scroll_top <= 2,
      can_scroll_back: scroll_top > 2,
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
    // The sensor inspected this table's local containers and found neither a pager nor a
    // scrollable collection surface. Page scrolling may reveal the table itself, but cannot
    // reveal additional rows inside it, so the rendered DOM rows form a bounded snapshot.
    return {{ type: 'static' }};
  }};

  // Page-level traversal sensor: deliberately not derived from a table/grid. Table traversal
  // keeps its own surface identity in snapshots[i].traversal; copying it here would let an
  // unrelated page-level consumer move that table without knowing which surface produced it.
  const detectPageViewport = () => {{
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
      return {{
        type: 'scroll',
        scroll_top: docTop,
        scroll_extent: docHeight,
        viewport_extent: viewH,
        at_scroll_start: docTop <= 2,
        can_scroll_back: docTop > 2,
        can_scroll_more,
        at_scroll_end: !can_scroll_more,
      }};
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
    // A data cell may contain a layout/detail table. Descendant selectors would
    // otherwise promote its rows and cells into the outer table, shifting columns
    // and creating synthetic records. Keep only nodes owned by this table/row.
    const ownedRows = (selector) => Array.from(table.querySelectorAll(selector))
      .filter((row) => row.closest("table") === table && visible(row));
    const ownedCells = (row) => Array.from(row.querySelectorAll("th,td"))
      .filter((cell) => cell.closest("tr") === row && visible(cell));
    const allRows = ownedRows("tr");
    if (!allRows.length) continue;
    let headerRow = ownedRows("thead tr").find((r) => ownedCells(r).some((cell) => text(cell)));
    if (!headerRow) headerRow = allRows.find((r) => ownedCells(r).some((cell) => cell.matches("th")));
    const headerCells = headerRow ? ownedCells(headerRow).slice(0, MAX_CELLS).map(text) : [];
    const bodyRows = ownedRows("tbody tr");
    const dataRows = (bodyRows.length ? bodyRows : allRows.filter((r) => r !== headerRow));
    const built = dataRows.map((r) => {{
      const cells = ownedCells(r);
      const only = cells.length === 1 ? cells[0] : null;
      const span = only
        ? Math.max(Number(only.colSpan || 1), Number(only.getAttribute("aria-colspan") || 1))
        : 1;
      return {{
        c: cells.slice(0, MAX_CELLS).map(text),
        l: cells.slice(0, MAX_CELLS).map(cellLink),
        placeholder: span > 1,
      }};
    }}).filter((b) => b.c.length > 0 && !b.placeholder);
    const rows = built.map((b) => b.c);
    const rowLinks = built.map((b) => b.l);
    if (!headerCells.length && rows.length < 2) continue;
    if (Math.max(headerCells.length, ...(rows.map((r) => r.length))) < 2) continue;
    const traversal = detectPagerState(table);
    snapshots.push(finalize({{
      source: "table",
      caption: nearbyTitle(table),
      headers: headerCells,
      rows,
      rowLinks,
      totalRecords: totalRecordsNear(table),
      path: uniquePath(table),
      traversal,
      ...viewportState(table),
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
      .map((r) => ({{
        c: cellsOf(r, '[role="gridcell"],[role="cell"],[role="rowheader"],td,th'),
        l: linksOf(r, '[role="gridcell"],[role="cell"],[role="rowheader"],td,th'),
      }}))
      .filter((b) => b.c.length > 0);
    const gridRows = rows.map((b) => b.c);
    const gridRowLinks = rows.map((b) => b.l);
    if (!headers.length && gridRows.length < 2) continue;
    if (Math.max(headers.length, ...gridRows.map((r) => r.length)) < 2) continue;
    const traversal = detectPagerState(grid);
    snapshots.push(finalize({{
      source: "aria-grid",
      caption: nearbyTitle(grid),
      headers,
      rows: gridRows,
      rowLinks: gridRowLinks,
      totalRecords: totalRecordsNear(grid),
      path: uniquePath(grid),
      traversal,
      ...viewportState(grid),
    }}));
  }}

  // Repeating list items (div/li cards) — structured data without a <table>.
  // Generic: a container holding >=2 structurally similar list items; fields come
  // from schema.org itemprop attributes, headings, and label+value pairs. This is
  // how a review/card/product list reads deterministically, so name-text pairing
  // and star ratings never depend on the vision model.
  const listVisible = (el) => {{
    if (!el || !el.isConnected) return false;
    const s = getComputedStyle(el);
    if (s.visibility === "hidden" || s.display === "none") {{
      return text(el).length > 0;  // data inside an inactive tab panel is still readable
    }}
    return true;
  }};
  const listItemSel = "li, [role='listitem']";
  for (const item of Array.from(document.querySelectorAll(listItemSel))) {{
    if (snapshots.length >= MAX_TABLES) break;
    if (!listVisible(item)) continue;
    let parent = item.parentElement;
    while (parent && parent.children.length < 2) parent = parent.parentElement;
    if (!parent || seen.has(parent)) continue;
    const siblings = Array.from(parent.children).filter((s) => listVisible(s) && s.matches(listItemSel));
    if (siblings.length < 2) continue;
    if (parent.querySelector("table")) continue;  // a real table already owns it
    const rows = [];
    const headerSet = [];
    for (const sib of siblings) {{
      const row = {{}};
      for (const el of sib.querySelectorAll("[itemprop]")) {{
        const key = norm(el.getAttribute("itemprop"));
        const val = text(el);
        if (key && val && !(key in row)) row[key] = val;
      }}
      const titleEl = sib.querySelector("h1,h2,h3,h4,h5,h6,[class*='title' i],[data-role='title']");
      if (titleEl) {{
        const t = text(titleEl);
        if (t && !Object.values(row).includes(t)) row["title"] = t;
      }}
      let best = "";
      for (const el of sib.querySelectorAll("p,div,[itemprop]")) {{
        const t = text(el);
        if (t.length > best.length && !Object.values(row).includes(t)) best = t;
      }}
      if (best) row["content"] = best;
      const ratingEl = sib.querySelector(".rating-result, [class*='rating' i], [itemprop='ratingValue']");
      const pctAttr = ratingEl
        ? (ratingEl.getAttribute("title") || ratingEl.getAttribute("aria-label") || text(ratingEl))
        : "";
      const pctMatch = pctAttr.match(/(\\d+(?:\\.\\d+)?)%/);
      if (pctMatch) {{
        // width percentage of a 5-star row -> star count (20% = 1, 80% = 4).
        row["ratingValue"] = Math.round(Number(pctMatch[1]) / 20 * 10) / 10;
      }}
      rows.push(row);
      for (const k of Object.keys(row)) if (!headerSet.includes(k)) headerSet.push(k);
    }}
    if (headerSet.length < 2 || rows.length < 2) continue;
    const listHeaders = headerSet;
    const traversal = detectPagerState(parent);
    snapshots.push(finalize({{
      source: "list",
      caption: nearbyTitle(parent),
      headers: listHeaders,
      rows: rows.map((r) => listHeaders.map((k) => (r[k] == null ? "" : r[k]))),
      rowLinks: rows.map(() => []),
      totalRecords: null,
      path: uniquePath(parent),
      traversal,
      ...viewportState(parent),
    }}));
    seen.add(parent);
  }}

  return JSON.stringify({{
    url: location.href,
    title: document.title,
    tables: snapshots,
    viewport: detectPageViewport(),
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
        # A cell's href is part of the row's complete content. Fold it in as a sibling column
        # "<col>_url" — no preset "the url" field; every linked column carries its own. Columns
        # must live in `headers` so every structured-data consumer sees the complete schema.
        raw_links = item.get("rowLinks") if isinstance(item.get("rowLinks"), list) else []
        link_headers = _link_headers(raw_links, headers, width)
        rows: list[dict[str, str]] = []
        for ridx, raw_row in enumerate(raw_rows):
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
            row = {headers[i]: (values[i] if i < len(values) else "") for i in range(width)}
            link_row = raw_links[ridx] if ridx < len(raw_links) and isinstance(raw_links[ridx], list) else []
            for col_idx, link_header in link_headers.items():
                href = str(link_row[col_idx]).strip() if col_idx < len(link_row) else ""
                row[link_header] = href
            rows.append(row)
        headers = headers + [link_headers[i] for i in sorted(link_headers)]
        total_records = _safe_int(item.get("totalRecords"))
        if total_records is not None and total_records < len(rows):
            total_records = None
        traversal = item.get("traversal")
        dom_row_count = _safe_int(item.get("domRows"))
        dom_row_count = len(rows) if dom_row_count is None else dom_row_count
        reconciled_total = bool(
            total_records is not None
            and isinstance(traversal, dict)
            and traversal.get("type") == "paged"
            and _safe_int(traversal.get("page_index")) == 1
            and _safe_int(traversal.get("page_count")) == 1
            and traversal.get("has_next_page") is False
            and dom_row_count == len(rows)
            and (_safe_int(traversal.get("page_size")) or 0) >= len(rows)
        )
        if reconciled_total:
            total_records = len(rows)
        out.append(
            {
                "index": idx,
                "source": str(item.get("source") or "table"),
                "caption": str(item.get("caption") or ""),
                "headers": headers,
                "rows": rows,
                "row_count": len(rows),
                "dom_row_count": dom_row_count,
                "total_records": total_records,
                "partial": bool(
                    not reconciled_total
                    and (item.get("partial") or (total_records and len(rows) < total_records))
                ),
                "path": str(item.get("path") or ""),
                "page": page,
                "in_viewport": item.get("in_viewport")
                if isinstance(item.get("in_viewport"), bool)
                else None,
                "viewport_pos": item.get("viewport_pos")
                if item.get("viewport_pos") in {"above", "below", "in"}
                else None,
                # Surface-scoped traversal evidence. Consumers must move this exact table rather
                # than borrowing the page-level viewport or another table's pager.
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


def _link_headers(raw_links: list[Any], headers: list[str], width: int) -> dict[int, str]:
    """Map each text-column index that carries any href to a unique "<col>_url" header name.

    A column is link-bearing if ANY row has a non-empty href in that cell; the whole column then
    gets a sibling URL column (rectangular: rows without a link store ""). Names are deduped
    against the existing text headers and each other so bindings see distinct fields."""
    has_link = [False] * width
    for link_row in raw_links:
        if not isinstance(link_row, list):
            continue
        for i in range(min(width, len(link_row))):
            if str(link_row[i] or "").strip():
                has_link[i] = True
    taken = set(headers)
    out: dict[int, str] = {}
    for i in range(width):
        if not has_link[i]:
            continue
        base = f"{headers[i]}_url"
        name, n = base, 1
        while name in taken:
            n += 1
            name = f"{base}_{n}"
        taken.add(name)
        out[i] = name
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
