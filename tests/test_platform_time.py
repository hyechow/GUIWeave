from __future__ import annotations

from types import SimpleNamespace

from gui_agent.adapters.android.device import AndroidDevice
from gui_agent.adapters.browser.device import PlaywrightDevice
from gui_agent.adapters.iphone.perception import IPhoneSession
from gui_agent.core.runtime.clock import PlatformTimeSnapshot, host_time_fallback


def test_browser_clock_uses_cdp_timezone_and_offset() -> None:
    device = PlaywrightDevice.__new__(PlaywrightDevice)
    device._cdp_send = lambda _method, _params: {
        "result": {
            "value": {
                "local_datetime": "2026-08-12T19:10:27+08:00",
                "timezone": "Asia/Shanghai",
                "utc_offset": "+08:00",
            }
        }
    }

    snapshot = device.platform_time()

    assert snapshot.platform == "browser"
    assert snapshot.source == "browser_cdp"
    assert snapshot.confidence == "authoritative"
    assert snapshot.local_datetime == "2026-08-12T19:10:27+08:00"
    assert snapshot.timezone == "Asia/Shanghai"


def test_android_clock_uses_adb_device_time() -> None:
    commands = {
        "date +%Y-%m-%dT%H:%M:%S%z": "2026-08-12T19:10:27+0800\n",
        "getprop persist.sys.timezone": "Asia/Shanghai\n",
    }
    device = AndroidDevice.__new__(AndroidDevice)
    device._dev = SimpleNamespace(shell=lambda command: commands[command])

    snapshot = device.platform_time()

    assert snapshot.platform == "android"
    assert snapshot.source == "android_adb"
    assert snapshot.local_datetime == "2026-08-12T19:10:27+08:00"
    assert snapshot.utc_offset == "+08:00"


def test_iphone_clock_explicitly_marks_host_fallback() -> None:
    snapshot = IPhoneSession().platform_time()

    assert snapshot.platform == "iphone"
    assert snapshot.source == "host_fallback"
    assert snapshot.confidence == "fallback"
    assert "no device clock channel" in snapshot.fallback_reason


def test_host_fallback_keeps_provenance() -> None:
    snapshot = host_time_fallback("browser", reason="test fallback")

    assert snapshot.local_datetime
    assert snapshot.utc_offset
    assert snapshot.fallback_reason == "test fallback"


def test_relative_date_offsets_use_frozen_platform_day() -> None:
    snapshot = PlatformTimeSnapshot(
        platform="browser",
        local_datetime="2026-08-14T23:59:59+08:00",
        timezone="Asia/Shanghai",
        utc_offset="+08:00",
        source="browser_cdp",
        confidence="authoritative",
        captured_at="2026-08-14T16:00:00.000+00:00",
    )

    offsets = snapshot.relative_date_offsets()
    assert offsets["-1"] == "2026-08-13"
    assert offsets["0"] == "2026-08-14"
    assert offsets["1"] == "2026-08-15"
