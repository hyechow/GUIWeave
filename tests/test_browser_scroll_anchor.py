from __future__ import annotations

from types import SimpleNamespace

from gui_agent.adapters.browser.device import (
    UNSAFE_SCROLL_SELECTOR,
    _CHOICE_OVERLAY_OPEN_JS,
    _POINT_HITS_CHOICE_JS,
    _UNSAFE_SCROLL_FOCUSABLE_SELECTOR,
    _UNSAFE_SCROLL_POPUP_SELECTOR,
    PlaywrightDevice,
)

_VENDOR_TOKENS = ("admin__", "mage-", "action-select", "selectmenu", "data-ui-id")


def test_unsafe_scroll_selector_is_generic_only() -> None:
    """Anchor exclusions must hold on arbitrary sites: tags, ARIA roles, and
    behavior-declaring attributes — never a vendor's CSS classes."""
    for token in _VENDOR_TOKENS:
        assert token not in UNSAFE_SCROLL_SELECTOR
        assert token not in _UNSAFE_SCROLL_POPUP_SELECTOR
        assert token not in _UNSAFE_SCROLL_FOCUSABLE_SELECTOR
    # Unconditional layer: interactive tags + ARIA widget roles.
    assert "select" in UNSAFE_SCROLL_SELECTOR.split(",")
    assert '[role="combobox"]' in UNSAFE_SCROLL_SELECTOR
    assert '[role="listbox"]' in UNSAFE_SCROLL_SELECTOR
    # Popup-semantics layer: a closed trigger declares haspopup/expanded — the
    # generic form of the "wheel opens the option list" failure class.
    assert "[aria-haspopup]" in _UNSAFE_SCROLL_POPUP_SELECTOR
    assert "[aria-expanded]" in _UNSAFE_SCROLL_POPUP_SELECTOR
    # Focusable non-widget surface: the generic closed-combobox shape.
    assert "[tabindex]" in _UNSAFE_SCROLL_FOCUSABLE_SELECTOR


def _device_with_page(page) -> PlaywrightDevice:
    dev = PlaywrightDevice.__new__(PlaywrightDevice)
    dev._page = page
    dev._last_viewport = (1280, 800)
    dev._follow_active_tab = lambda: None
    dev._require_page = lambda: page
    dev._cdp_send = lambda *args, **kwargs: (_ for _ in ()).throw(
        RuntimeError("cdp unused")
    )
    return dev


def test_choice_overlay_probe_ignores_persistent_aria_expanded() -> None:
    """Left-nav / Filters / accordions keep aria-expanded=true forever; they must not
    turn every tap into Escape and block Add Product / Search / Edit.

    The probe confirms popup semantics — explicit listbox/menu role with option
    descendants, or an expanded trigger (role=tab excluded) whose aria-controls
    target is visible AND floating — plus a structural channel for zero-ARIA
    widgets, all without site-family classes.
    """
    assert "choice_overlay_open" in _CHOICE_OVERLAY_OPEN_JS
    assert "getBoundingClientRect" in _CHOICE_OVERLAY_OPEN_JS
    assert "role === 'tab'" in _CHOICE_OVERLAY_OPEN_JS
    assert "aria-controls" in _CHOICE_OVERLAY_OPEN_JS
    assert "floating" in _CHOICE_OVERLAY_OPEN_JS
    assert '[role="listbox"], [role="menu"]' in _CHOICE_OVERLAY_OPEN_JS
    for token in _VENDOR_TOKENS:
        assert token not in _CHOICE_OVERLAY_OPEN_JS
        assert token not in _POINT_HITS_CHOICE_JS


def test_tap_dismisses_open_choice_overlay_instead_of_clicking() -> None:
    pressed: list[str] = []
    clicked: list[tuple[float, float]] = []

    # Overlay reports open (open-probe), but the tap point misses it (point-probe
    # False) → dismiss with Escape, then click.
    page = SimpleNamespace(
        bring_to_front=lambda: None,
        mouse=SimpleNamespace(click=lambda x, y: clicked.append((x, y))),
        keyboard=SimpleNamespace(press=lambda key: pressed.append(key)),
        evaluate=lambda expr, *args: "choice_overlay_open" in str(expr),
    )
    device = _device_with_page(page)

    result = device.tap(400.0, 700.0)

    assert result.startswith("OK tap")
    assert pressed == ["Escape"]
    assert clicked == [(400.0, 700.0)]


def test_tap_clicks_when_choice_overlay_is_closed() -> None:
    clicked: list[tuple[float, float]] = []
    page = SimpleNamespace(
        bring_to_front=lambda: None,
        mouse=SimpleNamespace(click=lambda x, y: clicked.append((x, y))),
        keyboard=SimpleNamespace(press=lambda key: None),
        evaluate=lambda expr, *args: False,
    )
    device = _device_with_page(page)

    result = device.tap(400.0, 700.0)

    assert result.startswith("OK tap")
    assert clicked == [(400.0, 700.0)]


def test_scroll_dismisses_choice_overlay_opened_by_wheel() -> None:
    pressed: list[str] = []
    moved: list[tuple[float, float]] = []
    wheeled: list[tuple[int, int]] = []
    page = SimpleNamespace(
        mouse=SimpleNamespace(
            move=lambda x, y: moved.append((x, y)),
            wheel=lambda dx, dy: wheeled.append((dx, dy)),
        ),
        keyboard=SimpleNamespace(press=lambda key: pressed.append(key)),
        evaluate=lambda expr, *args: "choice_overlay_open" in str(expr),
    )
    device = _device_with_page(page)

    result = device.scroll("down", 3, 200.0, 400.0)

    assert result.startswith("OK scroll")
    assert moved == [(200.0, 400.0)]
    assert wheeled
    assert pressed == ["Escape"]
