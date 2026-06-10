"""Deterministic unit tests for the android device + executor (mock adbutils).

No real phone, no adb binary: ``adbutils.AdbClient`` is monkeypatched with a fake
that records calls. These assert the load-bearing invariants — normalized 0-1000
coordinates map straight to DEVICE PIXELS with no window/retina offset, and the
keyevent mappings (home/back/recents/enter/wakeup) are correct.
"""

from __future__ import annotations

import types

import pytest
from PIL import Image


class _FakeDev:
    """Records the adb calls the device makes (one per adbutils convenience method)."""

    def __init__(self, calls: list, size=(1080, 2400)):
        self.calls = calls
        self._size = size

    def keyevent(self, code):
        self.calls.append(("key", int(code)))

    def click(self, x, y):
        self.calls.append(("click", x, y))

    def swipe(self, sx, sy, ex, ey, duration=None):
        self.calls.append(("swipe", sx, sy, ex, ey, duration))

    def shell(self, cmd):
        self.calls.append(("shell", cmd))
        return ""

    def window_size(self):
        return self._size

    def screenshot(self):
        return Image.new("RGB", self._size, "white")


class _FakeClient:
    def __init__(self, calls):
        self.calls = calls
        self._dev = _FakeDev(calls)

    def connect(self, addr, timeout=5.0):
        self.calls.append(("connect", addr))

    def device(self, serial=None):
        return self._dev

    def device_list(self):
        return [self._dev]


@pytest.fixture
def calls(monkeypatch):
    """Patch adbutils.AdbClient -> a fake recording client; yield the call log."""
    import adbutils

    recorded: list = []
    monkeypatch.setattr(adbutils, "AdbClient", lambda *a, **k: _FakeClient(recorded))
    return recorded


def _connected_device(serial="192.168.31.240:5555"):
    from gui_agent.adapters.android.device import AndroidDevice

    dev = AndroidDevice(serial=serial)
    dev.connect()
    return dev


def test_wireless_connect_and_wake(calls):
    dev = _connected_device()
    assert ("connect", "192.168.31.240:5555") in calls  # host:port -> adb connect
    assert ("key", 224) in calls  # KEYCODE_WAKEUP on connect
    assert (dev.win_w, dev.win_h) == (1080, 2400)  # cached from window_size()


def test_tap_passes_device_pixels_unchanged(calls):
    dev = _connected_device()
    dev.tap(540, 1200)
    assert ("click", 540, 1200) in calls  # straight through, no window/retina offset


def test_keyevent_mappings(calls):
    dev = _connected_device()
    dev.press_home()
    dev.back()
    dev.app_switch()
    dev.press_enter()
    assert ("key", 3) in calls    # HOME
    assert ("key", 4) in calls    # BACK
    assert ("key", 187) in calls  # APP_SWITCH
    assert ("key", 66) in calls   # ENTER


def test_screenshot_returns_png_bytes(calls):
    dev = _connected_device()
    png = dev.screenshot()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"  # PNG magic
    assert len(png) > 100


def test_scroll_down_swipes_finger_up_within_screen(calls):
    dev = _connected_device()
    dev.scroll("down", amount=5, x=540, y=1200)
    swipes = [c for c in calls if c[0] == "swipe"]
    assert swipes, "scroll should issue a swipe"
    _, sx, sy, ex, ey, _ = swipes[-1]
    assert sx == ex == 540          # vertical scroll keeps x
    assert sy > ey                  # see content below => finger swipes up
    assert 0 <= ey and sy <= 2400   # endpoints clamped inside the screen


def test_executor_denorm_maps_normalized_to_device_pixels(calls):
    from gui_agent.adapters.android.executor import AndroidExecutor

    dev = _connected_device()
    executor = AndroidExecutor(types.SimpleNamespace(client=dev))

    px, py = executor._denorm(500, 500)
    assert (round(px), round(py)) == (540, 1200)  # center of 1080x2400

    px2, py2 = executor._denorm(1000, 1000)
    assert (round(px2), round(py2)) == (1079, 2399)  # clamped to w-1, h-1
