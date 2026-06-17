from __future__ import annotations

import types

from gui_agent.adapters.browser.actions import BrowserAction, BrowserActionDecision
from gui_agent.adapters.browser.executor import BrowserExecutor
from gui_agent.adapters.browser.policies import BrowserActionPolicy


class _Client:
    viewport_size = (1000, 1000)

    def __init__(self, snap=(500.0, 500.0, "select 246x32")):
        self._snap = snap
        self.clicked = None
        self.selected = None

    def dom_snap(self, x, y, target_text=""):
        return self._snap

    def tap(self, x, y):
        self.clicked = (x, y)
        return f"OK tap ({x:.0f},{y:.0f})"

    def select_option(self, x, y, option_text):
        self.selected = (x, y, option_text)
        return f"OK select_option {option_text!r}"


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


def test_tap_on_native_select_with_option_text_is_rescued():
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
    assert client.selected == (856.0, 509.0, "Complete")
    assert client.clicked is None


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


def test_select_instruction_rewrites_tap_to_select_option():
    policy = BrowserActionPolicy()
    decision = BrowserActionDecision(
        action=BrowserAction(
            action_type="tap",
            x=850,
            y=504,
            description="点击状态筛选下拉列表，以选择'Complete'选项",
        )
    )

    result = policy._postprocess(decision, "点击下拉列表中的 'Complete' 选项以设置状态筛选条件")

    assert result.action.action_type == "select_option"
    assert result.action.text == "Complete"
    assert result.action.x == 850
    assert result.action.y == 504


def test_select_instruction_rewrites_unquoted_option_text():
    policy = BrowserActionPolicy()
    decision = BrowserActionDecision(
        action=BrowserAction(
            action_type="tap",
            x=850,
            y=504,
            description="点击 Status 下拉框",
        )
    )

    result = policy._postprocess(decision, "在 Status 下拉框选择 Complete")

    assert result.action.action_type == "select_option"
    assert result.action.text == "Complete"


def test_filter_button_tap_is_not_rewritten_to_select_option():
    policy = BrowserActionPolicy()
    decision = BrowserActionDecision(
        action=BrowserAction(
            action_type="tap",
            x=596,
            y=274,
            description="点击 Filters 按钮",
        )
    )

    result = policy._postprocess(
        decision,
        "点击 Filters 按钮以设置筛选条件 Status = Complete",
    )

    assert result.action.action_type == "tap"
    assert result.action.text is None
