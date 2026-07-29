from __future__ import annotations

from gui_agent.adapters.android.mobileworld import _init_task_then_wait_for_android
from gui_agent.core.runtime.factory import SetupCheckResult


class _FakeEnv:
    def __init__(self, events: list[str]):
        self.events = events

    def init_task(self, task_name: str) -> None:
        self.events.append(f"init:{task_name}")


def test_mobileworld_initializes_before_adb_probe_and_session_open():
    events: list[str] = []
    env = _FakeEnv(events)
    checks = iter(
        [
            SetupCheckResult(ok=False, summary="adb offline"),
            SetupCheckResult(ok=True, summary="android ready"),
        ]
    )

    def setup_check():
        events.append("probe")
        return next(checks)

    setup = _init_task_then_wait_for_android(
        env,  # type: ignore[arg-type]
        "CloseFlightModeTask",
        setup_check,
        ready_timeout_s=10,
        poll_s=1,
        monotonic=iter([0.0, 1.0]).__next__,
        sleep=lambda _seconds: events.append("sleep"),
    )

    assert setup.ok is True
    assert events == [
        "init:CloseFlightModeTask",
        "probe",
        "sleep",
        "probe",
    ]


def test_mobileworld_returns_last_failed_probe_at_ready_timeout():
    events: list[str] = []
    env = _FakeEnv(events)

    def setup_check():
        events.append("probe")
        return SetupCheckResult(ok=False, summary="adb offline")

    setup = _init_task_then_wait_for_android(
        env,  # type: ignore[arg-type]
        "CloseFlightModeTask",
        setup_check,
        ready_timeout_s=0,
        sleep=lambda _seconds: None,
    )

    assert setup.ok is False
    assert events == ["init:CloseFlightModeTask", "probe"]
