from __future__ import annotations

import types

from gui_agent.adapters.browser.actions import BrowserAction, BrowserActionDecision
from gui_agent.adapters.browser.executor import BrowserExecutor
from gui_agent.core.schemas import BaseAction, BaseActionDecision


class _Client:
    viewport_size = (1000, 1000)

    def __init__(self, snap=(500.0, 500.0, "select 246x32")):
        self._snap = snap
        self.clicked = None
        self.selected = None
        self.scrolled_ref = None

    def dom_snap(self, x, y, target_text=""):
        return self._snap

    def tap(self, x, y):
        self.clicked = (x, y)
        return f"OK tap ({x:.0f},{y:.0f})"

    def select_option(self, x, y, option_text):
        self.selected = (x, y, option_text)
        return f"OK select_option {option_text!r}"

    def scroll_to_ref(self, target_ref):
        self.scrolled_ref = target_ref
        return "ok"


def _exec(client: _Client) -> BrowserExecutor:
    return BrowserExecutor(types.SimpleNamespace(client=client))


def test_select_option_action_dispatches_client():
    client = _Client()
    decision = BrowserActionDecision(
        action=BrowserAction(
            action_type="select_option",
            x=600,
            y=500,
            text="Complete",
            description="在 Status 下拉框选择 Complete",
        )
    )

    assert _exec(client).execute(decision) is True
    assert client.selected == (600.0, 500.0, "Complete")
    assert client.clicked is None


def test_tool_agent_base_action_dispatches_browser_select_option():
    client = _Client()
    decision = BaseActionDecision(
        action=BaseAction(
            action_type="select_option",
            x=600,
            y=500,
            text="Complete",
            description="Choose the required status",
        )
    )

    assert _exec(client).execute(decision) is True
    assert client.selected == (600.0, 500.0, "Complete")
    assert client.clicked is None


def test_scroll_to_ref_dispatches_transport_without_click():
    client = _Client()
    decision = BrowserActionDecision(
        action=BrowserAction(
            action_type="scroll_to_ref",
            target_ref=247901,
            description="将 Add Swatch 移入视口",
        )
    )

    assert _exec(client).execute(decision) is True
    assert client.scrolled_ref == 247901
    assert client.clicked is None


def test_tap_on_native_select_never_becomes_an_implicit_write():
    client = _Client(snap=(856.0, 509.0, "select 246x32"))
    decision = BrowserActionDecision(
        action=BrowserAction(
            action_type="tap",
            x=850,
            y=504,
            description="点击状态筛选下拉列表，以选择'Complete'选项",
        )
    )

    assert _exec(client).execute(decision) is True
    assert client.selected is None
    assert client.clicked == (856.0, 509.0)

def test_plain_tap_on_select_still_clicks_when_no_option_text():
    client = _Client(snap=(856.0, 509.0, "select 246x32"))
    decision = BrowserActionDecision(
        action=BrowserAction(
            action_type="tap",
            x=850,
            y=504,
            description="点击 Status 筛选框",
        )
    )

    assert _exec(client).execute(decision) is True
    assert client.selected is None
    assert client.clicked == (856.0, 509.0)
