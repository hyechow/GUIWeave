from __future__ import annotations

import shutil
from pathlib import Path

from gui_agent.adapters.android import factory
from gui_agent.adapters.android import constants
from gui_agent.adapters.android import device as device_module


class _ReadyDevice:
    win_w = 1080
    win_h = 2400
    stay_awake_enabled = True

    def __init__(self, *, serial: str | None) -> None:
        self.serial = serial

    def connect(self) -> None:
        return None

    def ensure_adbkeyboard(self) -> tuple[bool, bool]:
        return True, True

    def close(self) -> None:
        return None


def test_android_preflight_reports_bundled_adb_and_scrcpy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    asset_dir = tmp_path / "scrcpy-macos-aarch64-v4.0"
    asset_dir.mkdir()
    adb = asset_dir / "adb"
    scrcpy = asset_dir / "scrcpy"
    adb.write_bytes(b"adb")
    scrcpy.write_bytes(b"scrcpy")
    adb.chmod(0o755)
    scrcpy.chmod(0o755)
    monkeypatch.delenv("ADBUTILS_ADB_PATH", raising=False)
    monkeypatch.setattr(constants, "VENDORED_ADB", adb)
    monkeypatch.setattr(device_module, "AndroidDevice", _ReadyDevice)
    monkeypatch.setattr(factory, "_is_apple_silicon", lambda: True)
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    result = factory._setup_check("device-1")

    assert result.ok
    assert any(f"adb 可执行文件: {adb}" in line for line in result.lines)
    assert any("adb 设备已连接" in line for line in result.lines)
    assert any("svc power stayon true" in line for line in result.lines)
    assert any("不会解除锁屏" in line for line in result.lines)
    assert any("svc power stayon false" in line for line in result.lines)
    assert any("scrcpy 可用" in line for line in result.lines)


def test_android_preflight_does_not_offer_arm64_scrcpy_on_intel(
    tmp_path: Path,
    monkeypatch,
) -> None:
    asset_dir = tmp_path / "scrcpy-macos-aarch64-v4.0"
    asset_dir.mkdir()
    adb = asset_dir / "adb"
    scrcpy = asset_dir / "scrcpy"
    adb.write_bytes(b"adb")
    scrcpy.write_bytes(b"scrcpy")
    adb.chmod(0o755)
    scrcpy.chmod(0o755)
    monkeypatch.delenv("ADBUTILS_ADB_PATH", raising=False)
    monkeypatch.delenv("GUIWEAVE_SCRCPY_PATH", raising=False)
    monkeypatch.setattr(constants, "VENDORED_ADB", adb)
    monkeypatch.setattr(device_module, "AndroidDevice", _ReadyDevice)
    monkeypatch.setattr(factory, "_is_apple_silicon", lambda: False)
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    result = factory._setup_check("device-1")

    assert result.ok
    assert any("仅支持 arm64" in line for line in result.lines)
    assert not any("scrcpy 可用" in line for line in result.lines)


def test_android_preflight_blocks_when_no_adb_binary(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("ADBUTILS_ADB_PATH", raising=False)
    monkeypatch.setattr(constants, "VENDORED_ADB", tmp_path / "missing-adb")
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    result = factory._setup_check(None)

    assert not result.ok
    assert result.summary == "adb 可执行文件不可用"
    assert any("插件内未找到可执行 adb" in line for line in result.lines)
