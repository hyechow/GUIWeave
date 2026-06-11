"""BrowserExecutor._tap wires device.dom_snap → clicks the snapped point and records action.snap.

The snap METRIC (device.dom_snap, elementFromPoint + conservative guards) needs a live browser,
so here we lock the executor wiring with a fake client: a snapped result moves the click, a
no-snap (info=None) keeps it, a snap failure falls back to the original point (never blocks a
click), and going through execute() records action.snap (normalized) so the report / runtime
visualizer draw original→snapped — the same as iphone YOLO/OCR.
"""

from __future__ import annotations

import types

from gui_agent.adapters.browser.actions import BrowserAction, BrowserActionDecision
from gui_agent.adapters.browser.executor import BrowserExecutor


class _FakeClient:
    def __init__(self, snap, viewport=(1000, 1000)):
        self._snap = snap  # (sx, sy, info) tuple OR an Exception to raise
        self._viewport = viewport
        self.clicked = None

    @property
    def viewport_size(self):
        return self._viewport

    def dom_snap(self, x, y):
        if isinstance(self._snap, Exception):
            raise self._snap
        return self._snap

    def tap(self, x, y):
        self.clicked = (x, y)
        return f"OK tap ({x:.0f},{y:.0f})"


def _exec(client) -> BrowserExecutor:
    return BrowserExecutor(types.SimpleNamespace(client=client))


def test_tap_snaps_to_dom_center():
    c = _FakeClient((635.0, 620.0, "div 666x28"))   # near-miss → snapped to row center
    assert _exec(c)._tap(342, 616) is True
    assert c.clicked == (635.0, 620.0)


def test_tap_no_snap_keeps_point():
    c = _FakeClient((342.0, 616.0, None))            # canvas / huge / no clickable → no snap
    _exec(c)._tap(342, 616)
    assert c.clicked == (342, 616)


def test_tap_snap_failure_falls_back_to_original():
    c = _FakeClient(RuntimeError("cdp down"))         # snap errored → never block the click
    _exec(c)._tap(342, 616)
    assert c.clicked == (342, 616)


def test_execute_records_dom_snap_normalized():
    # viewport 1000x1000 → pixels == normalized, easy to assert
    c = _FakeClient((635.0, 620.0, "div 666x28"), viewport=(1000, 1000))
    dec = BrowserActionDecision(action=BrowserAction(action_type="tap", x=342, y=616, description="点选项"))
    _exec(c).execute(dec)
    snap = dec.action.snap
    assert snap is not None and snap["method"] == "dom"
    assert snap["original"] == [342, 616]            # LLM's normalized coords
    assert snap["snapped"] == [635.0, 620.0]         # DOM center, normalized back
    assert c.clicked == (635.0, 620.0)


def test_execute_no_snap_leaves_action_snap_none():
    c = _FakeClient((342.0, 616.0, None), viewport=(1000, 1000))
    dec = BrowserActionDecision(action=BrowserAction(action_type="tap", x=342, y=616, description="点"))
    _exec(c).execute(dec)
    assert dec.action.snap is None
