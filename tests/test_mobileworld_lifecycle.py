from __future__ import annotations

import json
from types import SimpleNamespace

from gui_agent.adapters.android.mobileworld import (
    MOBILEWORLD_PACKAGE_MANAGER,
    _android_platform_contract,
    _build_parser,
    _final_answer,
    _generate_and_persist_reply,
    _guess_task_type,
    _init_task_then_wait_for_android,
    _route_mobileworld_goal,
)
from gui_agent.core.chat.session import RouterResult
from gui_agent.core.run.result import AgentResult
from gui_agent.core.runtime.factory import SetupCheckResult
from gui_agent.core.tool_agent.presentation import PresentationResult
from gui_agent.core.tool_agent.result import execute_tool_agent


class _FakeEnv:
    def __init__(self, events: list[str]):
        self.events = events

    def init_task(self, task_name: str) -> None:
        self.events.append(f"init:{task_name}")


def test_android_platform_contract_uses_semantic_app_names():
    app_names = ["Calendar", "Messages"]

    contract = _android_platform_contract(app_names)

    assert "Available application names" in contract
    assert '["Calendar", "Messages"]' in contract
    assert "org.fossify.calendar" not in contract


def test_mobileworld_package_manager_uses_internal_package_names():
    assert MOBILEWORLD_PACKAGE_MANAGER["Calendar"] == "org.fossify.calendar"
    assert MOBILEWORLD_PACKAGE_MANAGER["Messages"] == "com.google.android.apps.messaging"


def test_mobileworld_cli_accepts_tool_agent_runtime_options():
    parser = _build_parser()
    args = parser.parse_args([
        "OpenFlightModeTask",
        "--runtime",
        "tool-agent",
        "--perception",
        "vision-only",
        "--tool-agent-multi-action",
    ])

    assert args.task == "OpenFlightModeTask"
    assert args.runtime == "tool-agent"
    assert args.perception == "vision-only"
    assert args.tool_agent_multi_action is True
    defaults = parser.parse_args(["OpenFlightModeTask"])
    disabled = parser.parse_args([
        "OpenFlightModeTask",
        "--no-tool-agent-multi-action",
    ])

    assert defaults.runtime == "tool-agent"
    assert defaults.tool_agent_multi_action is True
    assert disabled.tool_agent_multi_action is False


def test_mobileworld_task_type_fallback_handles_state_mutations():
    assert _guess_task_type("Turn on device flight mode") == "MUTATE"
    assert _guess_task_type("How many alarms are configured?") == "RETRIEVE"


def test_mobileworld_routes_backend_goal_as_android_and_preserves_raw_separately():
    calls = []

    def route(goal, *, session, platform):
        calls.append((goal, session, platform))
        return RouterResult(goal="明确后的任务目标")

    routed, payload = _route_mobileworld_goal("raw goal", route=route)
    fallback, empty_payload = _route_mobileworld_goal(
        "raw goal",
        route=lambda *_args, **_kwargs: RouterResult(),
    )

    assert routed == "明确后的任务目标"
    assert payload["goal"] == routed
    assert calls == [("raw goal", [], "android")]
    assert fallback == "raw goal"
    assert empty_payload["goal"] == ""


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
