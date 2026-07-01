"""Offline regression for the settle-hang that froze WebArena sweep tasks 502/505/549.

Root cause: after CDP `wait_settled()` threw, the vision-fallback settle loop called
`platform.screenshot()` on the SAME wedged CDP connection, which blocked forever (the try/except
only catches raises, not hangs) — freezing the whole run until the external 600s kill.

Fix: every CDP settle/screenshot call in `settle_after_action` is now wrapped in a wall-clock
timeout (`_guarded_call`). These tests use a platform whose calls HANG (block on an Event) and
assert `settle_after_action` still returns quickly instead of hanging. The Event is released in a
finally so the leaked guard threads unblock and the test process can exit cleanly."""
from __future__ import annotations

import threading
import time

import pytest

from gui_agent.core.run import action_exec


class _WedgedPlatform:
    """wait_settled raises-or-hangs; screenshot hangs until `release` is set."""

    def __init__(self, release: threading.Event, settle_mode: str):
        self._release = release
        self._settle_mode = settle_mode

    def wait_settled(self, action_type=None):
        if self._settle_mode == "raise":
            raise RuntimeError("CDP settle boom")
        # "hang": block like a wedged connection until released
        self._release.wait(30)
        return 0.0, False

    def screenshot(self) -> bytes:
        self._release.wait(30)  # wedged CDP: blocks forever (30s test safety net)
        return b"frame"

    def pop_tab_switched(self) -> bool:
        return False


@pytest.mark.parametrize("settle_mode", ["raise", "hang"])
def test_settle_returns_bounded_when_cdp_calls_hang(settle_mode, monkeypatch):
    # shrink the guard so the test is fast; read at call time via SETTLE_CALL_TIMEOUT_S
    monkeypatch.setattr(action_exec, "SETTLE_CALL_TIMEOUT_S", 0.3)
    monkeypatch.setattr(action_exec, "SETTLE_FIRST_S", 0.01)
    monkeypatch.setattr(action_exec, "SETTLE_UNIT_S", 0.01)

    release = threading.Event()
    platform = _WedgedPlatform(release, settle_mode)
    try:
        t0 = time.perf_counter()
        elapsed, no_effect = action_exec.settle_after_action(
            platform, pre_frame=b"pre", action_type="tap"
        )
        dt = time.perf_counter() - t0
        # bounded: guard fires per hung call; NOT the old unbounded freeze
        assert dt < 5.0, f"settle_after_action hung for {dt:.1f}s (guard did not fire)"
        assert isinstance(elapsed, float)
    finally:
        release.set()  # unblock the leaked guard threads so the process can exit cleanly


def test_settle_normal_path_still_returns_cdp_result():
    # sanity: when wait_settled works, its result is returned unchanged (no timeout interference)
    class _GoodPlatform:
        def wait_settled(self, action_type=None):
            return 1.23, True

    elapsed, no_effect = action_exec.settle_after_action(
        _GoodPlatform(), pre_frame=b"pre", action_type="tap"
    )
    assert (elapsed, no_effect) == (1.23, True)
