from __future__ import annotations

from gui_agent.adapters.browser.actions import BrowserAction
from gui_agent.adapters.browser.visualizer import _visual_point


def test_browser_visualizer_prefers_snapped_point():
    action = BrowserAction(action_type="tap", x=342, y=616, description="点")
    assert _visual_point(action) == (342, 616)
    action.snap = {
        "method": "dom",
        "original": [342, 616],
        "snapped": [635.0, 620.0],
    }
    assert _visual_point(action) == (635.0, 620.0)
