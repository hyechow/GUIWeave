from __future__ import annotations

import json

from gui_agent.adapters.android.mobileworld import (
    _final_answer,
    _generate_and_persist_reply,
    _init_task_then_wait_for_android,
)
from gui_agent.core.run.result import AgentResult
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


def test_mobileworld_reply_is_separate_from_exact_evaluator_answer(
    monkeypatch,
    tmp_path,
):
    context_path = tmp_path / "context.json"
    context_path.write_text(
        json.dumps({
            "outcome": {
                "phase": "completed",
                "summary": "raw",
                "verification": "confirmed",
                "output": "42",
            },
            "reply": None,
        }),
        encoding="utf-8",
    )
    result = AgentResult(
        goal="Return the exact number",
        output="42",
        summary="completed",
        phase="completed",
        verification="confirmed",
    )
    monkeypatch.setattr(
        "gui_agent.core.llm.output.generate_reply",
        lambda goal, payload: f"reply for {goal}: {payload['output']}",
    )

    reply = _generate_and_persist_reply(context_path, result.goal, result)

    assert _final_answer(result) == "42"
    assert reply == "reply for Return the exact number: 42"
    persisted = json.loads(context_path.read_text(encoding="utf-8"))
    assert persisted["outcome"]["output"] == "42"
    assert persisted["reply"] == reply
