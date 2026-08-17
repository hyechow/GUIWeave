"""reveal_control capability: deterministic reveal of off-screen frame controls."""

from __future__ import annotations

from types import SimpleNamespace

from gui_agent.adapters.browser.actions import BrowserAction
from gui_agent.adapters.browser.executor import BrowserExecutor


def test_browser_action_allows_offscreen_positions_for_reveal_control() -> None:
    action = BrowserAction(
        action_type="reveal_control",
        x=500,
        y=1650,
        description="Edit Configurations button below the fold",
    )
    assert action.y == 1650
    action_above = BrowserAction(
        action_type="reveal_control", x=500, y=-300, description="toggle above",
    )
    assert action_above.y == -300


def test_browser_action_still_bounds_normal_actions_to_viewport() -> None:
    import pytest

    with pytest.raises(Exception, match="outside the normalized"):
        BrowserAction(action_type="tap", x=500, y=1650, description="tap")


def test_executor_dispatches_reveal_control_with_rect() -> None:
    calls = []
    client = SimpleNamespace(
        reveal_control=lambda control: calls.append(control) or "ok",
    )
    executor = BrowserExecutor.__new__(BrowserExecutor)
    executor.session = SimpleNamespace(client=client)

    action = BrowserAction(
        action_type="reveal_control", x=500, y=1650, description="below-fold button",
    )
    assert executor._dispatch_extra(action, client) is True
    assert calls == [{"rect": {"x": 500, "y": 1650}}]
