"""Deterministic unit tests for the android device + executor (mock adbutils).

No real phone, no adb binary: ``adbutils.AdbClient`` is monkeypatched with a fake
that records calls. These assert the load-bearing invariants — normalized 0-1000
coordinates map straight to DEVICE PIXELS with no window/retina offset, and the
keyevent mappings (home/back/recents/enter/wakeup) are correct.
"""

from __future__ import annotations

import types
from unittest.mock import Mock

import pytest
from PIL import Image


class _FakeDev:
    """Records the adb calls the device makes (one per adbutils convenience method)."""

    def __init__(self, calls: list, size=(1080, 2400), state="device"):
        self.calls = calls
        self._size = size
        self._state = state

    def get_state(self):
        self.calls.append(("get_state",))
        return self._state

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
    assert ("get_state",) in calls  # real transport probe, not just a device handle
    assert ("key", 224) in calls  # KEYCODE_WAKEUP on connect
    assert ("shell", "svc power stayon true") in calls
    assert dev._stay_awake_enabled is True
    assert (dev.win_w, dev.win_h) == (1080, 2400)  # cached from window_size()


def test_connect_rejects_offline_device_handle(monkeypatch):
    """adbutils.device(serial) returns a handle even when the transport is offline."""
    import adbutils

    calls: list = []
    client = _FakeClient(calls)
    client._dev = _FakeDev(calls, state="offline")
    monkeypatch.setattr(adbutils, "AdbClient", lambda *a, **k: client)

    from gui_agent.adapters.android.device import AndroidDevice

    with pytest.raises(RuntimeError, match=r"is offline .*expected device"):
        AndroidDevice(serial="192.168.1.102:5556").connect()
    assert ("get_state",) in calls
    assert ("key", 224) not in calls  # no input is sent through an unproven transport


def test_connect_uses_probe_when_wireless_refresh_raises(monkeypatch):
    """A failed best-effort `adb connect` must not mask an already-live transport."""
    import adbutils

    calls: list = []
    client = _FakeClient(calls)

    def fail_refresh(addr, timeout=5.0):
        calls.append(("connect_failed", addr))
        raise TimeoutError("refresh timed out")

    client.connect = fail_refresh
    monkeypatch.setattr(adbutils, "AdbClient", lambda *a, **k: client)

    from gui_agent.adapters.android.device import AndroidDevice

    dev = AndroidDevice(serial="192.168.1.102:5556").connect()
    assert dev._dev is client._dev
    assert ("get_state",) in calls


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


def test_list_apps_and_launch_semantic_name_from_package_manager():
    from gui_agent.adapters.android.device import AndroidDevice

    commands = []
    listing = """
2 activities found:
    com.example.notes/.MainActivity
    org.example.calendar/org.example.calendar.StartActivity
"""

    class _LauncherDev:
        def shell(self, command):
            commands.append(command)
            return listing if command.startswith("cmd package query-activities") else "Status: ok"

    package_manager = {
        "Calendar": "org.example.calendar",
        "Missing": "org.example.missing",
    }
    device = AndroidDevice(serial="device")
    device.package_manager = package_manager
    device._dev = _LauncherDev()

    assert device.list_apps() == ["Calendar"]
    component = "org.example.calendar/org.example.calendar.StartActivity"
    assert device.launch_app("Calendar") == "OK launch_app Calendar"
    assert commands[-1] == f"am start -W -n {component}"
    with pytest.raises(ValueError, match="Missing.*0 components"):
        device.launch_app("Missing")


def test_current_app_id_reads_foreground_package_without_input() -> None:
    from gui_agent.adapters.android.device import AndroidDevice

    commands = []

    class _ForegroundDev:
        def shell(self, command):
            commands.append(command)
            return (
                "mCurrentFocus=Window{123 u0 "
                "com.example.calendar/.MainActivity}"
            )

    device = AndroidDevice(serial="device")
    device._dev = _ForegroundDev()

    assert device.current_app_id() == "com.example.calendar"
    assert commands == ["dumpsys window windows"]


def test_screenshot_returns_png_bytes(calls):
    dev = _connected_device()
    png = dev.screenshot()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"  # PNG magic
    assert len(png) > 100


def test_screenshot_reconnects_once_after_wireless_transport_goes_offline() -> None:
    from gui_agent.adapters.android.device import AndroidDevice

    class _OfflineDev:
        def screenshot(self):
            raise RuntimeError("device offline")

        def shell(self, _command):
            raise RuntimeError("device offline")

    class _OnlineDev:
        def screenshot(self):
            return Image.new("RGB", (32, 64), "white")

    device = AndroidDevice(serial="192.168.1.102:5555")
    device._dev = _OfflineDev()
    reconnects = []

    def reconnect():
        reconnects.append(device.serial)
        device._dev = _OnlineDev()
        return device

    device._reconnect_transport = reconnect  # type: ignore[method-assign]

    assert device.screenshot().startswith(b"\x89PNG")
    assert reconnects == ["192.168.1.102:5555"]


def test_screenshot_does_not_reconnect_for_non_transport_failures() -> None:
    from gui_agent.adapters.android.device import AndroidDevice

    class _DeniedDev:
        def screenshot(self):
            raise PermissionError("screencap permission denied")

        def shell(self, _command):
            raise PermissionError("screencap permission denied")

    device = AndroidDevice(serial="192.168.1.102:5555")
    device._dev = _DeniedDev()
    reconnects = []
    device._reconnect_transport = lambda: reconnects.append(True)  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="permission denied"):
        device.screenshot()
    assert reconnects == []


def test_dump_ui_hierarchy_reads_direct_xml_without_a_remote_file() -> None:
    from gui_agent.adapters.android.device import AndroidDevice

    calls = []

    class _HierarchyDev:
        def shell(self, command, timeout=None):
            calls.append((command, timeout))
            return (
                '<?xml version="1.0"?><hierarchy><node text="Files" />'
                '</hierarchy>\nUI hierchary dumped to: /dev/tty'
            )

    device = AndroidDevice(serial="device")
    device._dev = _HierarchyDev()

    assert device.dump_ui_hierarchy() == (
        '<hierarchy><node text="Files" /></hierarchy>'
    )
    assert calls == [("uiautomator dump /dev/tty", 6.0)]


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
    """List scrolls keep wide range; picker scrolls use column-specific bounded units."""
    import types

    from gui_agent.adapters.android.actions import AndroidAction
    from gui_agent.adapters.android.executor import AndroidExecutor

    ex = AndroidExecutor(types.SimpleNamespace(client=None))
    assert ex._amount_units("small") == 1
    assert ex._amount_units("medium") == 4
    assert ex._amount_units("large") == 8
    minute_medium = AndroidAction(
        action_type="scroll",
        direction="down",
        amount="medium",
        snap={"picker_column": "minute"},
        description="滚动分钟列",
    )
    minute_large = minute_medium.model_copy(update={"amount": "large"})
    hour_medium = minute_medium.model_copy(update={"snap": {"picker_column": "hour"}})
    assert ex._amount_units_for_action(minute_medium) == 2
    assert ex._amount_units_for_action(minute_large) == 3
    assert ex._amount_units_for_action(hour_medium) == 2


def test_scroll_two_units_uses_slow_picker_speed(calls):
    dev = _connected_device()
    dev.scroll("down", amount=2, x=540, y=576)
    _, *_coords, duration = [c for c in calls if c[0] == "swipe"][-1]
    assert duration == 0.7


def test_scroll_three_units_uses_bounded_picker_coarse_speed(calls):
    dev = _connected_device()
    dev.scroll("down", amount=3, x=540, y=576)
    _, *_coords, duration = [c for c in calls if c[0] == "swipe"][-1]
    assert duration == 0.45


def test_executor_denorm_maps_normalized_to_device_pixels(calls):
    from gui_agent.adapters.android.executor import AndroidExecutor

    dev = _connected_device()
    executor = AndroidExecutor(types.SimpleNamespace(client=dev))

    px, py = executor._denorm(500, 500)
    assert (round(px), round(py)) == (540, 1200)  # center of 1080x2400

    px2, py2 = executor._denorm(1000, 1000)
    assert (round(px2), round(py2)) == (1079, 2399)  # clamped to w-1, h-1


def test_executor_grounds_named_android_menu_row_before_dispatch() -> None:
    from gui_agent.adapters.android.actions import AndroidAction, AndroidActionDecision
    from gui_agent.adapters.android.executor import AndroidExecutor

    executor = AndroidExecutor(types.SimpleNamespace(client=None))
    original = AndroidActionDecision(action=AndroidAction(
        action_type="tap",
        x=500,
        y=870,
        description="Tap the Create New Channel option in the bottom sheet",
    ))
    controls = [
        {
            "kind": "button",
            "label": "Browse Channels",
            "in_viewport": True,
            "rect": {"x": 500, "y": 850, "w": 1000, "h": 45},
        },
        {
            "kind": "button",
            "label": "Create New Channel",
            "in_viewport": True,
            "rect": {"x": 500, "y": 900, "w": 1000, "h": 45},
        },
    ]

    grounded = executor.ground_coordinates(original, controls)

    assert (grounded.action.x, grounded.action.y) == (500, 900)
    assert grounded.action.snap == {
        "method": "android_control_semantic_geometry",
        "original": [500.0, 870.0],
        "snapped": [500.0, 900.0],
        "info": "Create New Channel",
    }


def test_android_grounding_fails_open_for_ambiguous_named_controls() -> None:
    from gui_agent.adapters.android.actions import AndroidAction, AndroidActionDecision
    from gui_agent.adapters.android.executor import AndroidExecutor

    executor = AndroidExecutor(types.SimpleNamespace(client=None))
    original = AndroidActionDecision(action=AndroidAction(
        action_type="tap",
        x=500,
        y=500,
        description="Tap Continue",
    ))
    controls = [
        {
            "kind": "button",
            "label": "Continue",
            "rect": {"x": 450, "y": 500, "w": 80, "h": 40},
        },
        {
            "kind": "button",
            "label": "Continue",
            "rect": {"x": 550, "y": 500, "w": 80, "h": 40},
        },
    ]

    grounded = executor.ground_coordinates(original, controls)

    assert grounded is original
    assert grounded.action.snap is None


def test_android_grounding_prefers_nested_switch_widget_center() -> None:
    """A same-label switch row + child widget is one target, not ambiguity."""

    from gui_agent.adapters.android.actions import AndroidAction, AndroidActionDecision
    from gui_agent.adapters.android.executor import AndroidExecutor

    executor = AndroidExecutor(types.SimpleNamespace(client=None))
    original = AndroidActionDecision(action=AndroidAction(
        action_type="tap",
        x=950,
        y=175,
        description="Tap the visible Bluetooth toggle switch labeled 蓝牙",
    ))
    controls = [
        {
            "kind": "switch",
            "label": "蓝牙",
            "ref": "android:0.1",
            "rect": {"x": 500, "y": 145, "w": 1000, "h": 60},
        },
        {
            "kind": "switch",
            "label": "蓝牙",
            "ref": "android:0.1.0.1",
            "rect": {"x": 883, "y": 145, "w": 133, "h": 60},
        },
    ]

    grounded = executor.ground_coordinates(original, controls)

    assert (grounded.action.x, grounded.action.y) == (883, 145)
    assert grounded.action.snap == {
        "method": "android_control_semantic_nested_geometry",
        "original": [950.0, 175.0],
        "snapped": [883.0, 145.0],
        "info": "蓝牙",
    }


def test_android_grounding_keeps_same_label_sibling_switches_ambiguous() -> None:
    from gui_agent.adapters.android.actions import AndroidAction, AndroidActionDecision
    from gui_agent.adapters.android.executor import AndroidExecutor

    executor = AndroidExecutor(types.SimpleNamespace(client=None))
    original = AndroidActionDecision(action=AndroidAction(
        action_type="tap",
        x=500,
        y=500,
        description="Tap Notifications toggle switch",
    ))
    controls = [
        {
            "kind": "switch",
            "label": "Notifications",
            "ref": "android:0.1",
            "rect": {"x": 450, "y": 500, "w": 80, "h": 40},
        },
        {
            "kind": "switch",
            "label": "Notifications",
            "ref": "android:0.2",
            "rect": {"x": 550, "y": 500, "w": 80, "h": 40},
        },
    ]

    grounded = executor.ground_coordinates(original, controls)

    assert grounded is original
    assert grounded.action.snap is None


def test_android_grounding_snaps_wide_input_edge_to_center() -> None:
    from gui_agent.adapters.android.actions import AndroidAction, AndroidActionDecision
    from gui_agent.adapters.android.executor import AndroidExecutor

    executor = AndroidExecutor(types.SimpleNamespace(client=None))
    original = AndroidActionDecision(action=AndroidAction(
        action_type="type",
        x=911,
        y=294,
        text="reading",
        description="Type reading in the Name text input field",
    ))
    controls = [{
        "kind": "text_input",
        "label": "Bugs, Marketing",
        "rect": {"x": 501, "y": 294, "w": 820, "h": 27},
    }]

    grounded = executor.ground_coordinates(original, controls)

    assert (grounded.action.x, grounded.action.y) == (501, 294)
    assert grounded.action.snap["method"] == "android_control_geometry"


# --------------------------------------------------------------------------- #
# IME handling: per-connect detect (read-only) vs setup-time switch.           #
# ADBKeyboard is the preferred all-character input path. The SWITCH lives in   #
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


def test_type_text_prefers_adbkeyboard_for_ascii_and_chinese():
    """When active, ADBKeyboard handles every character set through one path."""
    from gui_agent.adapters.android.constants import ADBKEYBOARD_IME

    for text in ("hello world", "你好 hello 123"):
        dev = _device_with(_ImeFakeDev(installed=True, current_ime=ADBKEYBOARD_IME))
        dev._adbkeyboard_active = True
        assert dev.type_text(text).startswith("OK type")
        log = dev._dev.shell_log
        assert any("ADB_INPUT_B64" in s for s in log)
        assert not any("input text" in s for s in log)


def test_type_text_falls_back_to_input_text_for_ascii_without_adbkeyboard():
    inactive = _device_with(_ImeFakeDev(installed=False, current_ime="com.baidu.input/.X"))
    inactive._adbkeyboard_active = False
    assert inactive.type_text("hello world 123") == "OK type 'hello world 123'"
    assert any("input text hello%sworld%s123" in s for s in inactive._dev.shell_log)
    assert not any("ADB_INPUT_B64" in s for s in inactive._dev.shell_log)


def test_type_text_fails_non_ascii_when_adbkeyboard_not_ready():
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


def _execute_android(action, *, target_control="", **statuses):
    from gui_agent.adapters.android.actions import AndroidActionDecision
    from gui_agent.adapters.android.executor import AndroidExecutor

    client = Mock(viewport_size=(1080, 2400))
    for name in (
        "tap",
        "clear_text",
        "type_text",
        "press_enter",
        "scroll",
        "drag",
        "press_home",
        "back",
        "app_switch",
        "long_press",
        "launch_app",
    ):
        getattr(client, name).return_value = statuses.get(name, f"OK {name}")
    ok = AndroidExecutor(types.SimpleNamespace(client=client)).execute(
        AndroidActionDecision(action=action),
        target_control=target_control,
    )
    return ok, client


@pytest.mark.parametrize(
    ("action_kwargs", "status_name", "status"),
    [
        (
            {"action_type": "tap", "x": 500, "y": 500, "description": "点击"},
            "tap",
            "failed: offline",
        ),
        (
            {"action_type": "clear_text", "description": "清空"},
            "clear_text",
            "interrupted by user",
        ),
        (
            {"action_type": "press_enter", "description": "回车"},
            "press_enter",
            "paused: blocked",
        ),
        (
            {
                "action_type": "scroll",
                "direction": "down",
                "description": "滚动",
            },
            "scroll",
            "failed: offline",
        ),
        (
            {
                "action_type": "drag",
                "x": 100,
                "y": 200,
                "to_x": 800,
                "to_y": 200,
                "description": "拖动",
            },
            "drag",
            "failed: offline",
        ),
        (
            {"action_type": "home", "description": "主屏"},
            "press_home",
            "failed: offline",
        ),
        (
            {"action_type": "back", "description": "返回"},
            "back",
            "interrupted",
        ),
        (
            {"action_type": "app_switch", "description": "切换"},
            "app_switch",
            "paused",
        ),
        (
            {
                "action_type": "long_press",
                "x": 500,
                "y": 500,
                "description": "长按项目",
            },
            "long_press",
            "failed: offline",
        ),
        (
            {
                "action_type": "launch_app",
                "app": "Calendar",
                "description": "启动日历",
            },
            "launch_app",
            "failed: unavailable",
        ),
    ],
)
def test_executor_propagates_device_action_failures(action_kwargs, status_name, status):
    from gui_agent.adapters.android.actions import AndroidAction

    ok, _ = _execute_android(AndroidAction(**action_kwargs), **{status_name: status})
    assert ok is False


@pytest.mark.parametrize(
    ("target_control", "expected_x"),
    [("Brightness slider", 1079), ("Canvas object", 1034.208)],
    ids=("slider-snaps", "ordinary-drag-preserved"),
)
def test_slider_endpoint_normalization(target_control, expected_x):
    from gui_agent.adapters.android.actions import AndroidAction

    action = AndroidAction(
        action_type="drag",
        x=501.5,
        y=93.8,
        to_x=957.6,
        to_y=93.8,
        description="向右拖动",
    )
    ok, client = _execute_android(action, target_control=target_control)

    assert ok is True
    client.drag.assert_called_once()
    assert client.drag.call_args.args == pytest.approx(
        (541.62, 225.12, expected_x, 225.12, 1000)
    )


def test_executor_denormalizes_long_press_and_launches_semantic_app():
    from gui_agent.adapters.android.actions import AndroidAction

    pressed, press_client = _execute_android(AndroidAction(
        action_type="long_press",
        x=250,
        y=750,
        duration_ms=800,
        description="长按文件",
    ))
    launched, launch_client = _execute_android(AndroidAction(
        action_type="launch_app",
        app="Calendar",
        description="启动日历",
    ))

    assert pressed is launched is True
    press_client.long_press.assert_called_once_with(270, 1800, 800)
    launch_client.launch_app.assert_called_once_with("Calendar")


def test_type_stops_when_clear_fails_and_propagates_type_failure():
    from gui_agent.adapters.android.actions import AndroidAction

    action = AndroidAction(
        action_type="type",
        x=500,
        y=500,
        text="hello",
        description="输入 hello",
    )
    clear_ok, clear_client = _execute_android(action, clear_text="failed: offline")
    assert clear_ok is False
    clear_client.type_text.assert_not_called()

    type_ok, type_client = _execute_android(action, type_text="failed: IME unavailable")
    assert type_ok is False
    type_client.type_text.assert_called_once_with("hello")
