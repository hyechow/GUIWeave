"""Applied-filter state extraction — the deterministic "which filters are currently in
effect" signal, read from the grid's own Active-filters chips.

This is the authoritative answer to "did the filter ACTION take effect", decoupled from "do
the resulting rows look right" (the EFFECT). A `filter` milestone's job is to APPLY a filter;
its success is that the intended chip is present — NOT that the agent re-reads row/cell values
(where the checker once conflated an adjacent display column, e.g. Magento `Salable Quantity`,
with the filtered `Quantity` and rejected a correctly-applied filter into a clear→reset loop).

Magento admin grids render applied filters as chips under `.admin__current-filters-list`; each
chip is `<label>: <value>` (e.g. `Quantity: 3 - 3`, `Store View: Default Store View`). We
serialize them to `{label: value}`. Platform-specific (browser); core stays neutral via
`Observation.applied_filters`.
"""

from __future__ import annotations

import json
from typing import Any


def applied_filters_js() -> str:
    """A JS expression (run via CDP Runtime.evaluate, returnByValue) that serializes the grid's
    Active-filters chips to a JSON object string `{label: value}`. Returns `"{}"` when the page
    has no applied-filters bar (not a grid / nothing applied).

    Live DOM (Magento 2.4.6, verified): the bar is `<ul class="admin__current-filters-list"
    data-role="filter-list">`; each chip is a bare `<li>` holding a label span
    `<span data-bind="text: label + ':'">Quantity:</span>`, a value span (`3 - 3`), and a
    `<button class="action-remove">Remove</button>`. The chip carries NO `current-filters-item`
    class, and the button's "Remove" text pollutes `li.innerText` — so we read the label span
    directly and take the remaining textContent (label span + buttons removed) as the value."""
    return (
        "(()=>{"
        "const out={};"
        "const items=document.querySelectorAll("
        "'.admin__current-filters-list > li, [data-role=\"filter-list\"] > li');"
        "items.forEach(li=>{"
        "  const lblEl=li.querySelector('span[data-bind*=\"label\"]') || li.querySelector('span');"
        "  let label=(lblEl?(lblEl.textContent||''):'').replace(/\\s+/g,' ').replace(/[:：]\\s*$/,'').trim();"
        "  const clone=li.cloneNode(true);"
        "  clone.querySelectorAll('button, span[data-bind*=\"label\"]').forEach(n=>n.remove());"
        "  let value=(clone.textContent||'').replace(/\\s+/g,' ').trim();"
        "  if(label && value){out[label]=value;}"
        "});"
        "return JSON.stringify(out);"
        "})()"
    )


def normalize_applied_filters(raw: Any) -> dict[str, str] | None:
    """Coerce the JS result (a JSON string or already-parsed dict) into `{label: value}` with
    trimmed string keys/values. Returns None when empty/unusable — so `Observation.applied_filters`
    stays None on non-grid pages (None = no signal, distinct from {} = a grid with no filters)."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return None
    if not isinstance(raw, dict) or not raw:
        return None
    out: dict[str, str] = {}
    for k, v in raw.items():
        key = str(k).strip()
        if not key:
            continue
        out[key] = str(v).strip()
    return out or None
