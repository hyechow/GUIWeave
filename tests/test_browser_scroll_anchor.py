from __future__ import annotations

import pytest

from gui_agent.adapters.browser.actions import BrowserAction, BrowserActionDecision
from gui_agent.adapters.browser.device import PlaywrightDevice
from gui_agent.adapters.browser.executor import BrowserExecutor
from gui_agent.adapters.browser.scroll_probe import BrowserScrollProfile, apply_profile


class _Client:
    viewport_size = (1280, 960)

    def __init__(self) -> None:
        self.anchor_calls: list[tuple[float, float, str]] = []
        self.scroll_calls: list[tuple[str, int, float, float]] = []

    def safe_scroll_anchor(self, x: float, y: float, area: str):
        self.anchor_calls.append((x, y, area))
        return 1000.0, 500.0, "div"

    def scroll(self, direction: str, amount: int, x: float, y: float) -> str:
        self.scroll_calls.append((direction, amount, x, y))
        return "OK"


class _Session:
    def __init__(self, client) -> None:
        self.client = client


def _scroll_action(**updates) -> BrowserAction:
    values = {
        "action_type": "scroll",
        "direction": "down",
        "amount": "medium",
        "target_area": "main_content",
        "description": "滚动查看下方内容",
    }
    values.update(updates)
    return BrowserAction(**values)


def test_coordinate_free_scroll_uses_non_control_dom_anchor():
    client = _Client()
    executor = BrowserExecutor(_Session(client))
    action = _scroll_action()

    ok = executor.execute(BrowserActionDecision(action=action))

    assert ok is True
    assert client.anchor_calls == [(640.0, 480.0, "main_content")]
    assert len(client.scroll_calls) == 1
    direction, amount, x, y = client.scroll_calls[0]
    assert (direction, amount, x) == ("down", 5, 1000.0)
    assert y == pytest.approx(500.0)
    assert action.x == 781.25
    assert action.y == 500.0 / 960.0 * 1000.0


def test_explicit_local_scroll_anchor_is_not_rewritten():
    client = _Client()
    executor = BrowserExecutor(_Session(client))
    action = _scroll_action(x=250, y=400, target_area="left_panel")

    ok = executor.execute(BrowserActionDecision(action=action))

    assert ok is True
    assert client.anchor_calls == []
    assert client.scroll_calls == [("down", 5, 320.0, 384.0)]


def test_cached_scroll_profile_recomputes_anchor_on_each_frame():
    action = _scroll_action(x=250, y=400)

    cached = apply_profile(
        action,
        BrowserScrollProfile(x=900, y=700, direction="down"),
    )

    assert cached.x is None
    assert cached.y is None
    assert cached.direction == "down"


class _Page:
    def __init__(self) -> None:
        self.expression = ""
        self.args = None

    def evaluate(self, expression, args):
        self.expression = expression
        self.args = args
        return {"x": 900, "y": 410, "tag": "section", "score": 12}


def test_device_scroll_anchor_probe_excludes_interactive_controls():
    page = _Page()
    device = object.__new__(PlaywrightDevice)
    device._follow_active_tab = lambda: None
    device._require_page = lambda: page

    result = device.safe_scroll_anchor(640, 480, "right_panel")

    assert result == (900.0, 410.0, "section")
    assert page.args == {
        "preferredX": 640.0,
        "preferredY": 480.0,
        "area": "right_panel",
    }
    assert "'select'" in page.expression
    assert "el.closest(unsafe)" in page.expression
