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


def test_scroll_anchored_near_top_stays_off_gesture_zone(calls):
    """A picker scroll anchored high must NOT start the swipe at the very top edge —
    that pulls down the HarmonyOS notification shade / Control Center and derails."""
    dev = _connected_device()  # 1080x2400
    dev.scroll("up", amount=8, x=750, y=150)  # large amount, anchored high
    _, sx, sy, ex, ey, _ = [c for c in calls if c[0] == "swipe"][-1]
    inset = max(120, 2400 // 12)  # gesture-safe band
    assert sy >= inset and ey >= inset            # both endpoints off the top zone
    assert sy <= 2400 - inset and ey <= 2400 - inset


def test_android_executor_amount_units_widen_range():
    """small must be a fine wheel-picker nudge (1 unit), large a list fling (8)."""
    import types

    from gui_agent.adapters.android.executor import AndroidExecutor

    ex = AndroidExecutor(types.SimpleNamespace(client=None))
    assert ex._amount_units("small") == 1
    assert ex._amount_units("medium") == 4
    assert ex._amount_units("large") == 8


def test_executor_denorm_maps_normalized_to_device_pixels(calls):
    from gui_agent.adapters.android.executor import AndroidExecutor

    dev = _connected_device()
    executor = AndroidExecutor(types.SimpleNamespace(client=dev))

    px, py = executor._denorm(500, 500)
    assert (round(px), round(py)) == (540, 1200)  # center of 1080x2400

    px2, py2 = executor._denorm(1000, 1000)
    assert (round(px2), round(py2)) == (1079, 2399)  # clamped to w-1, h-1


# --------------------------------------------------------------------------- #
# IME handling: per-connect detect (read-only) vs setup-time switch.           #
# ADBKeyboard is the non-ASCII (Chinese) input path. The SWITCH lives in        #
# ensure_adbkeyboard (env setup / setup_check); the per-connect _detect_ime     #
# only OBSERVES, never mutates the IME.                                         #
# --------------------------------------------------------------------------- #
class _ImeFakeDev:
    """Fake adb device modelling the IME-relevant shell commands (settings get /
    pm list packages / ime enable / ime set), tracking the live IME so a switch
    is observable via re-read."""

    def __init__(self, *, installed: bool, current_ime: str):
        self.installed = installed
        self.current_ime = current_ime
        self.shell_log: list[str] = []

    def shell(self, cmd):
        s = cmd if isinstance(cmd, str) else " ".join(cmd)
        self.shell_log.append(s)
        if s.startswith("settings get secure default_input_method"):
            return self.current_ime + "\n"
        if s.startswith("pm list packages"):
            return "package:com.android.adbkeyboard\n" if self.installed else ""
        if s.startswith("ime set "):
            self.current_ime = s.split("ime set ", 1)[1].strip()  # the switch takes effect
            return ""
        return ""


def _device_with(dev):
    from gui_agent.adapters.android.device import AndroidDevice

    d = AndroidDevice(serial="x:5555")
    d._dev = dev
    return d


def test_detect_ime_read_only_sets_flag_true_when_already_adbkeyboard():
    from gui_agent.adapters.android.constants import ADBKEYBOARD_IME

    fake = _ImeFakeDev(installed=True, current_ime=ADBKEYBOARD_IME)
    d = _device_with(fake)
    d._detect_ime()
    assert d._adbkeyboard_active is True
    assert not any(s.startswith("ime set") for s in fake.shell_log)  # never mutates


def test_detect_ime_false_for_other_ime_and_does_not_switch():
    fake = _ImeFakeDev(installed=True, current_ime="com.baidu.input/.X")
    d = _device_with(fake)
    d._detect_ime()
    assert d._adbkeyboard_active is False
    assert fake.current_ime == "com.baidu.input/.X"  # connect path leaves IME untouched
    assert not any(s.startswith("ime set") for s in fake.shell_log)


def test_ensure_adbkeyboard_switches_when_installed():
    from gui_agent.adapters.android.constants import ADBKEYBOARD_IME

    fake = _ImeFakeDev(installed=True, current_ime="com.baidu.input/.X")
    d = _device_with(fake)
    assert d.ensure_adbkeyboard() == (True, True)
    assert fake.current_ime == ADBKEYBOARD_IME
    assert d._adbkeyboard_active is True


def test_ensure_adbkeyboard_noop_when_not_installed():
    fake = _ImeFakeDev(installed=False, current_ime="com.baidu.input/.X")
    d = _device_with(fake)
    assert d.ensure_adbkeyboard() == (False, False)
    assert fake.current_ime == "com.baidu.input/.X"  # not switched
    assert d._adbkeyboard_active is False
    assert not any(s.startswith("ime set") for s in fake.shell_log)


def test_type_text_single_adbkeyboard_path_for_ascii_and_chinese():
    """ADBKeyboard is the ONE input method: both ASCII and non-ASCII go through the
    ADB_INPUT_B64 broadcast — no `input text` / ASCII-vs-Chinese split."""
    from gui_agent.adapters.android.constants import ADBKEYBOARD_IME

    for text in ("hello world", "你好 hello 123"):
        dev = _device_with(_ImeFakeDev(installed=True, current_ime=ADBKEYBOARD_IME))
        dev._adbkeyboard_active = True
        assert dev.type_text(text).startswith("OK type")
        log = dev._dev.shell_log
        assert any("ADB_INPUT_B64" in s for s in log)
        assert not any("input text" in s for s in log)  # never the old ASCII path


def test_type_text_fails_clearly_when_adbkeyboard_not_ready():
    inactive = _device_with(_ImeFakeDev(installed=False, current_ime="com.baidu.input/.X"))
    inactive._adbkeyboard_active = False
    out = inactive.type_text("你好")
    assert out.startswith("failed")
    assert not any("ADB_INPUT_B64" in s for s in inactive._dev.shell_log)


def test_clear_text_uses_adbkeyboard_native_broadcast_when_active():
    from gui_agent.adapters.android.constants import ADBKEYBOARD_IME

    active = _device_with(_ImeFakeDev(installed=True, current_ime=ADBKEYBOARD_IME))
    active._adbkeyboard_active = True
    assert active.clear_text() == "OK clear"
    assert any("ADB_CLEAR_TEXT" in s for s in active._dev.shell_log)
    assert not any("keyevent" in s for s in active._dev.shell_log)  # not the DEL-loop fallback

    inactive = _device_with(_ImeFakeDev(installed=True, current_ime="com.baidu.input/.X"))
    inactive._adbkeyboard_active = False
    assert inactive.clear_text() == "OK clear"
    assert any("keyevent" in s for s in inactive._dev.shell_log)  # fallback DEL loop
