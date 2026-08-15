from __future__ import annotations

from types import SimpleNamespace

from gui_agent.adapters.browser.device import (
    UNSAFE_SCROLL_SELECTOR,
    _CHOICE_OVERLAY_OPEN_JS,
    PlaywrightDevice,
)


def test_unsafe_scroll_selector_covers_magento_choice_widgets() -> None:
    assert ".admin__action-multiselect" in UNSAFE_SCROLL_SELECTOR
    assert ".action-select" in UNSAFE_SCROLL_SELECTOR
    assert '[aria-haspopup="listbox"]' in UNSAFE_SCROLL_SELECTOR
    assert "select" in UNSAFE_SCROLL_SELECTOR.split(",")


def _device_with_page(page) -> PlaywrightDevice:
    dev = PlaywrightDevice.__new__(PlaywrightDevice)
    dev._page = page
    dev._follow_active_tab = lambda: None
    dev._require_page = lambda: page
    dev._cdp_send = lambda *args, **kwargs: (_ for _ in ()).throw(
        RuntimeError("cdp unused")
    )
    return dev


def test_choice_overlay_probe_ignores_generic_aria_expanded() -> None:
    """Left-nav / Filters / Columns keep aria-expanded=true on the Products grid.

    A blanket [aria-expanded=true] probe turns every tap into Escape and blocks
    Add Product, Search by keyword, and Edit.
    """
    assert "choice_overlay_open" in _CHOICE_OVERLAY_OPEN_JS
    assert "getBoundingClientRect" in _CHOICE_OVERLAY_OPEN_JS
    assert '[aria-haspopup="listbox"][aria-expanded="true"]' in _CHOICE_OVERLAY_OPEN_JS
    assert "querySelector('[aria-expanded=\"true\"]')" not in _CHOICE_OVERLAY_OPEN_JS
    assert 'querySelector("[aria-expanded=\\"true\\"]")' not in _CHOICE_OVERLAY_OPEN_JS


def test_tap_dismisses_open_choice_overlay_instead_of_clicking() -> None:
    pressed: list[str] = []
    clicked: list[tuple[float, float]] = []

    page = SimpleNamespace(
        bring_to_front=lambda: None,
        mouse=SimpleNamespace(click=lambda x, y: clicked.append((x, y))),
        keyboard=SimpleNamespace(press=lambda key: pressed.append(key)),
        evaluate=lambda expr, *args: (
            True
            if "aria-expanded" in str(expr) or "activeElement" in str(expr)
            else False
        ),
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
        evaluate=lambda expr, *args: (
            True
            if "aria-expanded" in str(expr) or "activeElement" in str(expr)
            else None
        ),
    )
    device = _device_with_page(page)

    result = device.scroll("down", 3, 200.0, 400.0)

    assert result.startswith("OK scroll")
    assert moved == [(200.0, 400.0)]
    assert wheeled
    assert pressed == ["Escape"]
