"""Applied-filter state extraction — the deterministic "which filters are currently in
effect" signal behind ``Observation.applied_filters``.

This is the authoritative answer to "did the filter ACTION take effect", decoupled from "do
the resulting rows look right" (the EFFECT). A `filter` statement's job is to APPLY a filter;
its success is that the intended filter is in the page's applied-filter state — NOT that the
agent re-reads row/cell values (where the checker once conflated an adjacent display column,
e.g. Magento `Salable Quantity`, with the filtered `Quantity` and rejected a correctly-applied
filter into a clear→reset loop).

Magento admin has two grid families:

- modern UI-component grids render applied filters as chips under `.admin__current-filters-list`;
- legacy Mage_Adminhtml grids keep the applied filter in a `/filter/<base64>/` URL segment and
  render the filter row values under table headers.

Both are normalized to `{label: value}`. Platform-specific selectors live here; core stays
neutral via `Observation.applied_filters`.
"""

from __future__ import annotations

import json
from typing import Any

from gui_agent.core.tool_agent.filter_state import (
    AppliedFilterState,
    compile_filter_predicates,
)


def applied_filters_js() -> str:
    """A JS expression (run via CDP Runtime.evaluate, returnByValue) that serializes the grid's
    applied-filter state. It returns a JSON string with shape
    `{filters: {label: value}, meta: {...}}`.

    Live DOM (Magento 2.4.6, verified): the bar is `<ul class="admin__current-filters-list"
    data-role="filter-list">`; each chip is a bare `<li>` holding a label span
    `<span data-bind="text: label + ':'">Quantity:</span>`, a value span (`3 - 3`), and a
    `<button class="action-remove">Remove</button>`. The chip carries NO `current-filters-item`
    class, and the button's "Remove" text pollutes `li.innerText` — so we read the label span
    directly and take the remaining textContent (label span + buttons removed) as the value."""
    return r"""
(() => {
  const clean = (s) => String(s ?? '').replace(/\s+/g, ' ').trim();
  const out = {};
  const meta = {
    source: 'none',
    indicator_channel: 'absent',
    fallback_channel: 'absent',
    chip_container: 'absent',
    legacy_grid: 'absent',
  };

  const chipContainers = Array.from(document.querySelectorAll(
    '.admin__current-filters-list, [data-role="filter-list"]'
  ));
  if (chipContainers.length) {
    meta.indicator_channel = 'present';
    meta.chip_container = 'present';
  }

  const items = document.querySelectorAll(
    '.admin__current-filters-list > li, [data-role="filter-list"] > li'
  );
  items.forEach(li => {
    const lblEl = li.querySelector('span[data-bind*="label"]') || li.querySelector('span');
    const label = clean(lblEl ? (lblEl.textContent || '') : '').replace(/[:：]\s*$/, '').trim();
    const clone = li.cloneNode(true);
    clone.querySelectorAll('button, span[data-bind*="label"]').forEach(n => n.remove());
    const value = clean(clone.textContent || '');
    if (label && value) out[label] = value;
  });
  if (Object.keys(out).length) {
    meta.source = 'chips';
    return JSON.stringify({filters: out, meta});
  }

  const rendered = (el) => {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
  };
  const headerLabelFor = (el) => {
    const table = el.closest('table');
    if (!table) return '';
    const er = el.getBoundingClientRect();
    const cx = er.left + er.width / 2;
    const candidates = Array.from(table.querySelectorAll('th')).map(th => {
      const r = th.getBoundingClientRect();
      const text = clean(th.innerText || th.textContent || '')
        .replace(/[↑↓↕]+/g, '')
        .replace(/\s+/g, ' ')
        .trim();
      return {th, r, text};
    }).filter(c =>
      c.text && c.r.width > 0 && c.r.height > 0
      && c.r.left - 2 <= cx && cx <= c.r.right + 2
      && c.r.bottom <= er.top + 4
      && !c.th.querySelector('input,select,textarea')
    );
    candidates.sort((a, b) => b.r.bottom - a.r.bottom);
    return candidates[0] ? candidates[0].text : '';
  };
  const legacyTables = Array.from(document.querySelectorAll('table')).filter(table => {
    if (!rendered(table)) return false;
    const id = table.id || '';
    const staticGrid = Boolean(table.closest('.admin__data-grid-wrap-static'));
    const filterControl = table.querySelector(
      'input[id*="_filter_"], select[id*="_filter_"], textarea[id*="_filter_"]'
    );
    return Boolean(filterControl && (staticGrid || /_table$/.test(id)));
  });

  const legacy = (() => {
    const m = location.href.match(/\/filter\/([^/?#]*)/);
    if (!m) return null;
    const token = m[1] || '';
    if (!token) return {};
    try {
      let b64 = token.replace(/-/g, '+').replace(/_/g, '/');
      while (b64.length % 4) b64 += '=';
      const decoded = decodeURIComponent(atob(b64));
      const params = new URLSearchParams(decoded);
      const obj = {};
      for (const [k, v] of params.entries()) {
        if (!v) continue;
        if (/\[locale\]$/.test(k)) continue;
        obj[k] = v;
      }
      return obj;
    } catch (_e) {
      meta.fallback_channel = 'read_failed';
      meta.legacy_grid = 'read_failed';
      return null;
    }
  })();
  if (legacy === null && !legacyTables.length) {
    return JSON.stringify({filters: {}, meta});
  }
  meta.fallback_channel = 'present';
  meta.legacy_grid = 'present';

  const legacyOut = {};
  const allControls = Array.from(
    document.querySelectorAll('input,select,textarea')
  ).filter(rendered);
  if (legacy !== null) {
    for (const [key, value] of Object.entries(legacy || {})) {
      const control = allControls.find(el => {
        const name = el.getAttribute('name') || '';
        const id = el.id || '';
        return name === key || id === key || name.endsWith('[' + key + ']') || id.endsWith('_' + key);
      });
      const label = control ? (headerLabelFor(control) || clean(control.getAttribute('aria-label') || control.getAttribute('title'))) : '';
      legacyOut[label || key] = value;
    }
  } else {
    // Some Magento legacy grids persist filters in the server session and render the filtered
    // page at the plain grid URL.  In that shape there is no chip and no `/filter/<token>` URL.
    // A server-rendered control is distinguishable from an unsent local edit because its current
    // value still equals the HTML defaultValue/defaultSelected state.  Only those pristine,
    // non-empty controls are promoted to applied state; locally typed but unsubmitted values are
    // deliberately ignored until Search/Apply causes a fresh server render.
    const renderedFilters = legacyTables.flatMap(table =>
      Array.from(table.querySelectorAll(
        'input[id*="_filter_"], select[id*="_filter_"], textarea[id*="_filter_"]'
      )).filter(rendered)
    );
    const pairs = [];
    for (const control of renderedFilters) {
      const tag = control.tagName.toLowerCase();
      const value = tag === 'select'
        ? Array.from(control.selectedOptions || [])
            .map(o => clean(o.textContent || o.label || o.value))
            .filter(Boolean)
            .join(', ')
        : clean(control.value);
      if (!value) continue;
      const pristine = tag === 'select'
        ? Array.from(control.options || []).every(o => o.selected === o.defaultSelected)
        : control.value === control.defaultValue;
      if (!pristine) continue;
      const label = headerLabelFor(control)
        || clean(control.getAttribute('aria-label') || control.getAttribute('title'));
      if (label) pairs.push([label, value]);
    }
    const counts = pairs.reduce((acc, [label]) => {
      acc[label] = (acc[label] || 0) + 1;
      return acc;
    }, {});
    for (const [label, value] of pairs) {
      // Multiple controls under one header usually encode a range. Without an explicit encoded
      // state channel, do not guess how those values compose.
      if (counts[label] === 1) legacyOut[label] = value;
    }
    if (Object.keys(legacyOut).length) meta.source = 'legacy_rendered';
  }
  if (legacy !== null && Object.keys(legacyOut).length) meta.source = 'legacy_grid';
  return JSON.stringify({filters: legacyOut, meta});
})()
"""


def normalize_applied_filter_state(raw: Any) -> tuple[dict[str, str] | None, dict[str, Any]]:
    """Normalize the JS result to `(filters, meta)`.

    Accepts both the current `{filters, meta}` shape and the historical bare `{label: value}`
    mapping for compatibility with tests/callers.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return None, {
                "source": "read_failed",
                "indicator_channel": "unknown",
                "fallback_channel": "unknown",
                "chip_container": "unknown",
                "legacy_grid": "unknown",
            }
    if not isinstance(raw, dict):
        return None, {
            "source": "read_failed",
            "indicator_channel": "unknown",
            "fallback_channel": "unknown",
            "chip_container": "unknown",
            "legacy_grid": "unknown",
        }
    meta_raw = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
    filters_raw = raw.get("filters") if isinstance(raw.get("filters"), dict) else raw
    # Historical bare mapping callers should not leak their "meta" key as a filter.
    if filters_raw is raw and "filters" in raw:
        filters_raw = {}
    meta: dict[str, Any] = {
        "source": str(meta_raw.get("source") or ("unknown" if raw else "none")).strip() or "unknown",
        "indicator_channel": str(meta_raw.get("indicator_channel") or "unknown").strip() or "unknown",
        "fallback_channel": str(meta_raw.get("fallback_channel") or "unknown").strip() or "unknown",
        "chip_container": str(meta_raw.get("chip_container") or "unknown").strip() or "unknown",
        "legacy_grid": str(meta_raw.get("legacy_grid") or "unknown").strip() or "unknown",
    }
    filters = _normalize_filter_mapping(filters_raw)
    if not filters and meta["source"] == "unknown":
        meta["source"] = "none"
    return filters, meta


def normalize_applied_filters(raw: Any) -> dict[str, str] | None:
    """Coerce the JS result into `{label: value}` with trimmed string keys/values.

    Returns None when empty/unusable — so `Observation.applied_filters` stays None on pages
    where the adapter has no applied-filter signal.
    """
    filters, _meta = normalize_applied_filter_state(raw)
    return filters


def normalize_applied_filter_meta(raw: Any) -> dict[str, Any] | None:
    """Return only the meta portion from an applied-filter JS result."""
    _filters, meta = normalize_applied_filter_state(raw)
    return meta or None


def typed_applied_filter_state(
    filters: dict[str, str] | None,
    meta: dict[str, Any] | None,
) -> AppliedFilterState:
    """Project browser extraction evidence into the neutral typed state contract."""
    metadata = meta or {}
    source = str(metadata.get("source") or "").strip()
    complete_channel = bool(
        filters
        or source in {"chips", "legacy_url", "legacy_controls"}
        or str(metadata.get("indicator_channel") or "") == "present"
        or str(metadata.get("fallback_channel") or "") == "present"
    )
    return AppliedFilterState(
        predicates=compile_filter_predicates(
            filters or {},
            display_numeric_ranges=True,
        ),
        coverage="complete" if complete_channel else "unavailable",
        source=source,
    )


def _normalize_filter_mapping(raw: Any) -> dict[str, str] | None:
    if not isinstance(raw, dict) or not raw:
        return None
    out: dict[str, str] = {}
    for k, v in raw.items():
        if k in {"filters", "meta"}:
            continue
        key = str(k).strip()
        if not key:
            continue
        value = str(v).strip()
        if not value:
            continue
        out[key] = value
    return out or None
