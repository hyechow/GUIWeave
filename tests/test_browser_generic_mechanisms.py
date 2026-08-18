"""Generality invariant: browser adapter mechanisms must not encode site-family facts.

The agent operates on arbitrary sites, so every DOM probe is built from generic
signals — ARIA roles/states, semantic HTML, structural heuristics, behavioral
probing. Vendor CSS classes (Magento `.admin__*` / `.mage-*`, `data-ui-id`, ...)
are forbidden in adapter code, and evaluations are only the regression net for
this invariant — never the design target. See CLAUDE.md "Boundaries".
"""

from __future__ import annotations

import re

from gui_agent.adapters.browser import device
from gui_agent.adapters.browser.device import (
    _CHOICE_OVERLAY_OPEN_JS,
    _OPEN_CHOICE_POPUPS_JS,
    _POINT_HITS_CHOICE_JS,
    _UNSAFE_SCROLL_FOCUSABLE_SELECTOR,
    _UNSAFE_SCROLL_POPUP_SELECTOR,
    UNSAFE_SCROLL_SELECTOR,
)
from gui_agent.adapters.browser.executor import _JQUERY_DATEPICKER_SET_JS
from gui_agent.adapters.browser.filter_state import applied_filters_js
from gui_agent.adapters.browser.form_reader import form_controls_js
from gui_agent.adapters.browser.table_reader import table_snapshot_js

# Vendor namespaces and site-family hooks. Descriptive vocabulary tokens
# (title/pager/filter/required/datepicker/selectmenu) are allowed — they name UI
# widget types, not a vendor; this list is the vendor layer only.
_VENDOR_PATTERNS = (
    r"admin__",
    r"\bmage-",
    r"\bdata-ui-id\b",
    r"\bselectmenu-item",      # one widget implementation's option classes
    r"action-select\b",
    r"action-menu\b",
    r"_has-datepicker\b",
    r"required-entry\b",
    r"dashboard-item-title",
    r"advanced-select",
)

_ALL_ADAPTER_JS = {
    "form_controls_js": form_controls_js(),
    "table_snapshot_js": table_snapshot_js(),
    "applied_filters_js": applied_filters_js(),
    "jquery_datepicker_set_js": _JQUERY_DATEPICKER_SET_JS,
    "open_choice_popups_js": _OPEN_CHOICE_POPUPS_JS,
    "choice_overlay_open_js": _CHOICE_OVERLAY_OPEN_JS,
    "point_hits_choice_js": _POINT_HITS_CHOICE_JS,
}


def _device_source_js() -> str:
    import inspect

    return inspect.getsource(device)


def test_no_vendor_selectors_in_any_adapter_js() -> None:
    for name, js in _ALL_ADAPTER_JS.items():
        for pattern in _VENDOR_PATTERNS:
            assert not re.search(pattern, js), f"{name} embeds site-family token {pattern!r}"
    source = _device_source_js()
    for pattern in _VENDOR_PATTERNS:
        assert not re.search(pattern, source), f"device.py embeds site-family token {pattern!r}"


def test_anchor_exclusions_are_layered_generic_signals() -> None:
    assert "[aria-haspopup]" in _UNSAFE_SCROLL_POPUP_SELECTOR
    assert "[aria-expanded]" in _UNSAFE_SCROLL_POPUP_SELECTOR
    assert "[tabindex]" in _UNSAFE_SCROLL_FOCUSABLE_SELECTOR
    assert '[role="combobox"]' in UNSAFE_SCROLL_SELECTOR
    assert '[role="listbox"]' in UNSAFE_SCROLL_SELECTOR


def test_filter_state_uses_generic_channels() -> None:
    js = _ALL_ADAPTER_JS["applied_filters_js"]
    # Applied-filter chips: data-role / descriptive current-applied-active tokens.
    assert '[data-role="filter-list"]' in js
    assert "current-filters" in js
    assert "applied-filters" in js
    # Legacy channel: encoded /filter/<token>/ URL segment + pristine filter-row controls.
    assert "/filter/" in js
    assert "_filter_" in js


def test_form_reader_detects_filters_and_widgets_generically() -> None:
    js = _ALL_ADAPTER_JS["form_controls_js"]
    # Filter region: data-role/data-part/class "filter" container that holds no result grid.
    assert '[data-role*="filter" i]' in js
    assert '[class*="filter" i]' in js
    assert 'table,[role="grid"]' in js
    # Required/date/selectmenu are vocabulary-level widget signals.
    assert '[aria-required="true"]' in js
    assert '[class*="datepicker" i]' in js
    assert '[class*="selectmenu" i]' in js
    # Container-label sniff skips decorative text (errors/prefixes/hints).
    assert "decorative" in js


def test_table_reader_pager_and_titles_are_generic() -> None:
    js = _ALL_ADAPTER_JS["table_snapshot_js"]
    assert '[class*="pager" i]' in js
    assert "[class*='title' i]" in js
    # Current-page input: label[for] linkage and generic id/name patterns.
    assert "label" in js
    assert 'input[id*="page-current" i]' in js
