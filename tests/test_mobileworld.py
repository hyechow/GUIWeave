from __future__ import annotations

import subprocess

import pytest


def _cp(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["adb"], returncode, stdout=stdout, stderr="")


def test_wait_for_adb_ready_requires_stable_checks(monkeypatch):
    from gui_agent.adapters.android import mobileworld

    now = [0.0]
    sleeps: list[float] = []
    connects: list[str] = []
    commands: list[tuple[str, ...]] = []
    screencaps: list[str] = []

    monkeypatch.setattr(mobileworld.time, "monotonic", lambda: now[0])

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    def fake_connect(serial: str, *, timeout: float = 5.0):
        connects.append(serial)
        return _cp("connected\n")

    def fake_run_adb(serial: str, args: list[str], *, timeout: float = 5.0):
        commands.append(tuple(args))
        if args == ["get-state"]:
            return _cp("device\n")
        if args == ["shell", "getprop", "sys.boot_completed"]:
            return _cp("1\n")
        if args == ["shell", "wm", "size"]:
            return _cp("Physical size: 1080x2400\n")
        raise AssertionError(args)

    monkeypatch.setattr(mobileworld, "_adb_connect", fake_connect)
    monkeypatch.setattr(mobileworld, "_run_adb", fake_run_adb)
    monkeypatch.setattr(
        mobileworld,
        "_adb_screencap_ready",
        lambda serial: (screencaps.append(serial), (True, "screencap ok"))[1],
    )
    monkeypatch.setattr(mobileworld.time, "sleep", fake_sleep)

    mobileworld._wait_for_adb_ready("192.168.31.57:5556", timeout_s=10, interval_s=2)

    assert connects == ["192.168.31.57:5556", "192.168.31.57:5556"]
    assert screencaps == ["192.168.31.57:5556", "192.168.31.57:5556"]
    assert sleeps == [2]
    assert commands.count(("get-state",)) == 2


def test_wait_for_adb_ready_times_out_with_last_state(monkeypatch):
    from gui_agent.adapters.android import mobileworld

    now = [0.0]
    monkeypatch.setattr(mobileworld.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(mobileworld.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    monkeypatch.setattr(mobileworld, "_adb_connect", lambda *a, **k: _cp("connected\n"))

    def fake_run_adb(serial: str, args: list[str], *, timeout: float = 5.0):
        if args == ["get-state"]:
            return _cp("offline\n", returncode=1)
        if args == ["shell", "getprop", "sys.boot_completed"]:
            return _cp("")
        if args == ["shell", "wm", "size"]:
            return _cp("")
        raise AssertionError(args)

    monkeypatch.setattr(mobileworld, "_run_adb", fake_run_adb)

    with pytest.raises(RuntimeError, match="state='offline'"):
        mobileworld._wait_for_adb_ready("192.168.31.57:5556", timeout_s=3, interval_s=1)


def test_wait_for_adb_ready_requires_valid_screencap(monkeypatch):
    from gui_agent.adapters.android import mobileworld

    now = [0.0]
    monkeypatch.setattr(mobileworld.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(mobileworld.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    monkeypatch.setattr(mobileworld, "_adb_connect", lambda *a, **k: _cp("connected\n"))

    def fake_run_adb(serial: str, args: list[str], *, timeout: float = 5.0):
        if args == ["get-state"]:
            return _cp("device\n")
        if args == ["shell", "getprop", "sys.boot_completed"]:
            return _cp("1\n")
        if args == ["shell", "wm", "size"]:
            return _cp("Physical size: 1080x2400\n")
        raise AssertionError(args)

    monkeypatch.setattr(mobileworld, "_run_adb", fake_run_adb)
    monkeypatch.setattr(mobileworld, "_adb_screencap_ready", lambda serial: (False, "screencap not png"))

    with pytest.raises(RuntimeError, match="screencap not png"):
        mobileworld._wait_for_adb_ready("192.168.31.57:5556", timeout_s=3, interval_s=1)


def test_configure_android_http_proxy_sets_global_settings(monkeypatch):
    from gui_agent.adapters.android import mobileworld

    calls: list[tuple[str, tuple[str, ...]]] = []

    def fake_run_adb(serial: str, args: list[str], *, timeout: float = 5.0):
        calls.append((serial, tuple(args)))
        return _cp("")

    monkeypatch.setattr(mobileworld, "_run_adb", fake_run_adb)

    mobileworld._configure_android_http_proxy("192.168.31.57:5556", "http://10.0.2.2:38888")

    assert calls == [
        (
            "192.168.31.57:5556",
            ("shell", "settings", "put", "global", "http_proxy", "10.0.2.2:38888"),
        ),
        (
            "192.168.31.57:5556",
            ("shell", "settings", "put", "global", "global_http_proxy_host", "10.0.2.2"),
        ),
        (
            "192.168.31.57:5556",
            ("shell", "settings", "put", "global", "global_http_proxy_port", "38888"),
        ),
    ]


def test_normalize_android_proxy_rejects_missing_port():
    from gui_agent.adapters.android import mobileworld

    with pytest.raises(ValueError, match="host:port"):
        mobileworld._normalize_android_proxy("10.0.2.2")


def test_mobileworld_init_retries_defaults_to_one(monkeypatch):
    from gui_agent.adapters.android import mobileworld

    monkeypatch.delenv("MW_INIT_RETRIES", raising=False)

    assert mobileworld._mobileworld_init_retries() == 1


def test_run_emulator_restart_cmd_obeys_off(monkeypatch):
    from gui_agent.adapters.android import mobileworld

    calls = []
    monkeypatch.setenv("MW_EMULATOR_RESTART_CMD", "off")
    monkeypatch.setattr(mobileworld.subprocess, "run", lambda *a, **k: calls.append((a, k)))

    assert mobileworld._run_emulator_restart_cmd() is False
    assert calls == []


def test_run_emulator_restart_cmd_executes_split_command(monkeypatch):
    from gui_agent.adapters.android import mobileworld

    calls = []

    def fake_run(cmd, *, check: bool, timeout: float):
        calls.append((cmd, check, timeout))
        return _cp("")

    monkeypatch.setenv("MW_EMULATOR_RESTART_CMD", "docker exec mobile_world_env_0 true")
    monkeypatch.setenv("MW_EMULATOR_RESTART_TIMEOUT", "12")
    monkeypatch.setattr(mobileworld.subprocess, "run", fake_run)

    assert mobileworld._run_emulator_restart_cmd() is True
    assert calls == [(["docker", "exec", "mobile_world_env_0", "true"], True, 12.0)]
