from __future__ import annotations

import json
from types import SimpleNamespace

from gui_agent.adapters.android.mobileworld import (
    MOBILEWORLD_PACKAGE_MANAGER,
    _build_parser,
    _final_answer,
    _guess_task_type,
    _init_task_then_wait_for_android,
    _mobileworld_access_context,
)
from gui_agent.core.runtime.result import AgentResult
from gui_agent.core.runtime.factory import SetupCheckResult
from gui_agent.core.tool_agent.presentation import PresentationResult
from gui_agent.core.tool_agent.result import execute_tool_agent


class _FakeEnv:
    def __init__(self, events: list[str]):
        self.events = events

    def init_task(self, task_name: str) -> None:
        self.events.append(f"init:{task_name}")


def test_mobileworld_package_manager_uses_internal_package_names():
    assert MOBILEWORLD_PACKAGE_MANAGER["Calendar"] == "org.fossify.calendar"
    assert MOBILEWORLD_PACKAGE_MANAGER["Messages"] == "com.google.android.apps.messaging"


def test_mobileworld_combines_private_deployment_context_for_bound_apps() -> None:
    context = _mobileworld_access_context([
        SimpleNamespace(deployment="Service A access facts"),
        SimpleNamespace(deployment=""),
        SimpleNamespace(deployment="  Service B access facts  "),
    ])

    assert context == "Service A access facts\n\nService B access facts"


def test_mobileworld_cli_accepts_tool_agent_options():
    args = _build_parser().parse_args([
        "OpenFlightModeTask",
        "--perception",
        "vision-only",
        "--multi-action",
    ])

    assert args.task == "OpenFlightModeTask"
    assert args.perception == "vision-only"
    assert args.multi_action is True


def test_mobileworld_enables_tool_agent_multi_action_by_default() -> None:
    enabled = _build_parser().parse_args(["OpenFlightModeTask"])
    disabled = _build_parser().parse_args([
        "OpenFlightModeTask",
        "--no-multi-action",
    ])

    assert enabled.multi_action is True
    assert disabled.multi_action is False


def test_mobileworld_uses_full_task_turn_budget_by_default() -> None:
    assert _build_parser().parse_args(["OpenFlightModeTask"]).max_turns == 50


def test_mobileworld_task_type_fallback_handles_state_mutations():
    assert _guess_task_type("Turn on device flight mode") == "MUTATE"
    assert _guess_task_type("How many alarms are configured?") == "RETRIEVE"


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


def test_mobileworld_preserves_exact_evaluator_answer():
    result = AgentResult(
        goal="Return the exact number",
        output="42",
        summary="completed",
        phase="completed",
        verification="confirmed",
    )
    assert _final_answer(result) == "42"


def test_tool_agent_execution_persists_android_mobileworld_context(
    tmp_path,
    monkeypatch,
):
    runtime_run_kwargs = {}
    run = SimpleNamespace(
        phase="completed",
        effect="mutation",
        output=True,
        summary="Flight mode is enabled",
        result_ref=None,
        trace=[],
        perception_mode="enhanced",
        master_model="master",
        worker_model="worker",
        perception_model="perception",
    )
    presentation = PresentationResult(
        status="generated",
        reply="Flight mode is enabled.",
        result_digest="digest",
        model="presenter",
    )

    class FakeRuntime:
        def __init__(self, **kwargs):
            self.log_dir = kwargs["log_dir"]

        def run(self, _intent, **_kwargs):
            runtime_run_kwargs.update(_kwargs)
            (self.log_dir / "tool_agent_replay.json").write_text(
                '{"status":"passed"}', encoding="utf-8"
            )
            return run

    monkeypatch.setattr(
        "gui_agent.core.tool_agent.result.ToolAgentRuntime", FakeRuntime
    )
    monkeypatch.setattr(
        "gui_agent.core.tool_agent.result.present_result",
        lambda **_kwargs: presentation,
    )
    result, rendered = execute_tool_agent(
        intent="Turn on device flight mode",
        bundle=SimpleNamespace(platform="android"),
        session=object(),
        log_dir=tmp_path,
        perception_mode="enhanced",
        max_turns=10,
        allow_multi_action=False,
        fallback_task_type="MUTATE",
        knowledge_summary=None,
        access_context="Account `private-user` / password `private-secret`",
        raw_input="Turn on device flight mode",
        router={"goal": "Turn on device flight mode"},
    )

    context = json.loads((tmp_path / "context.json").read_text(encoding="utf-8"))
    assert rendered is presentation
    assert result.task_type == "MUTATE"
    assert result.orchestrator["kind"] == "tool_agent"
    assert context["platform"] == "android"
    assert context["orchestrator"]["kind"] == "tool_agent"
    assert context["reply"] == "Flight mode is enabled."
    assert runtime_run_kwargs["access_context"] == (
        "Account `private-user` / password `private-secret`"
    )
    assert "private-user" not in json.dumps(context)
    assert "private-secret" not in json.dumps(context)
