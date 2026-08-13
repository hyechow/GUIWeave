from __future__ import annotations

import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from gui_agent.core.config.preflight import ModelPreflightResult
from gui_agent.core.runtime.factory import SetupCheckResult
from gui_agent.core.runtime.io import get_log_root
from gui_agent.core.self_learning import app_summary
from gui_agent.core.tool_agent.service import (
    ToolAgentService,
    ToolAgentServiceResult,
)


def test_service_result_is_json_serializable() -> None:
    result = ToolAgentServiceResult(
        run_id="tool_agent/browser/20260812_120000",
        run_dir="/tmp/run",
        platform="browser",
        phase="completed",
        task_type="RETRIEVE",
        summary="done",
        output={"plan": "developer"},
        reply="The plan is developer.",
        context_path="/tmp/run/context.json",
        trace_path="/tmp/run/tool_agent_trace.json",
        replay_path="/tmp/run/tool_agent_replay.json",
        report_path="/tmp/run/report.html",
    )

    assert json.loads(json.dumps(result.to_dict()))["output"] == {
        "plan": "developer"
    }


def test_get_run_reads_only_artifacts_below_log_root(tmp_path: Path) -> None:
    service = ToolAgentService(log_root=tmp_path)
    run_id = "tool_agent/browser/20260812_120000"
    run_dir = tmp_path / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "context.json").write_text(
        json.dumps({
            "goal": "inspect the current page",
            "platform": "browser",
            "outcome": {"phase": "completed"},
            "reply": "done",
            "orchestrator": {"kind": "tool_agent"},
        }),
        encoding="utf-8",
    )
    (run_dir / "report.html").write_text("<html></html>", encoding="utf-8")

    result = service.get_run(run_id)

    assert result["run_dir"] == str(run_dir)
    assert result["orchestrator"] == {"kind": "tool_agent"}
    assert result["report_path"] == str(run_dir / "report.html")
    assert result["phase"] == "completed"
    assert result["artifacts"]["report"] == str(run_dir / "report.html")


def test_list_runs_includes_live_trace_and_orders_newest_first(tmp_path: Path) -> None:
    service = ToolAgentService(log_root=tmp_path)
    older = tmp_path / "tool_agent" / "browser" / "20260812_120000"
    newer = tmp_path / "tool_agent" / "android" / "20260812_130000"
    older.mkdir(parents=True)
    newer.mkdir(parents=True)
    (older / "context.json").write_text(
        json.dumps({"goal": "old", "platform": "browser", "outcome": {"phase": "completed"}}),
        encoding="utf-8",
    )
    (newer / "tool_agent_trace.json").write_text(
        json.dumps({"phase": "running", "summary": "observing", "trace": [{"event": "observe"}]}),
        encoding="utf-8",
    )

    runs = service.list_runs()

    assert [run["goal"] for run in runs] == ["", "old"]
    assert runs[0]["phase"] == "running"
    assert runs[0]["platform"] == "android"
    assert runs[0]["event_count"] == 1


def test_run_events_and_artifacts_are_bounded_and_allowlisted(tmp_path: Path) -> None:
    service = ToolAgentService(log_root=tmp_path)
    run_id = "tool_agent/browser/20260812_120000"
    run_dir = tmp_path / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "tool_agent_trace.json").write_text(
        json.dumps({"phase": "running", "trace": [{"index": 1}, {"index": 2}]}),
        encoding="utf-8",
    )
    (run_dir / "stdout.log").write_text("ready", encoding="utf-8")

    assert service.get_run_events(run_id, limit=1) == [{"index": 2}]
    assert service.get_artifact_path(run_id, "stdout") == run_dir / "stdout.log"
    with pytest.raises(ValueError, match="unsupported artifact"):
        service.get_artifact_path(run_id, "../../.env")


def test_run_frames_are_image_only_and_must_be_referenced(tmp_path: Path) -> None:
    service = ToolAgentService(log_root=tmp_path)
    run_id = "tool_agent/browser/20260812_120000"
    run_dir = tmp_path / run_id
    run_dir.mkdir(parents=True)
    frame = run_dir / "screenshot_tool_agent_1.png"
    frame.write_bytes(b"\x89PNG\r\n\x1a\n")
    (run_dir / "not-referenced.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (run_dir / "tool_agent_events.jsonl").write_text(
        json.dumps({"event": "observe", "screenshot_path": str(frame)}) + "\n",
        encoding="utf-8",
    )

    assert service.get_run_frame_path(run_id, frame.name) == frame
    with pytest.raises(ValueError, match="not referenced"):
        service.get_run_frame_path(run_id, "not-referenced.png")
    with pytest.raises(ValueError, match="unsupported"):
        service.get_run_frame_path(run_id, "../private.png")


def test_get_run_reads_incremental_jsonl_before_final_trace(tmp_path: Path) -> None:
    service = ToolAgentService(log_root=tmp_path)
    run_id = "tool_agent/browser/20260812_120000"
    run_dir = tmp_path / run_id
    run_dir.mkdir(parents=True)
    events = [
        {"index": 1, "event": "runtime_started", "goal": "inspect account"},
        {"index": 2, "event": "observe"},
    ]
    (run_dir / "tool_agent_events.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )

    result = service.get_run(run_id)

    assert result["phase"] == "running"
    assert result["goal"] == "inspect account"
    assert result["event_count"] == 2
    assert service.get_run_events(run_id, limit=1) == [events[-1]]


def test_get_run_rejects_path_traversal(tmp_path: Path) -> None:
    service = ToolAgentService(log_root=tmp_path / "logs")

    with pytest.raises(ValueError, match="outside"):
        service.get_run("../../private-run")


def test_service_resolves_log_root_after_environment_is_loaded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = tmp_path / "from-dotenv"
    monkeypatch.setenv("GUIWEAVE_LOG_ROOT", str(configured))

    assert get_log_root() == configured.resolve()
    assert ToolAgentService().log_root == configured.resolve()


def test_check_environment_combines_model_and_platform_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ToolAgentService(log_root=tmp_path)
    monkeypatch.setattr(
        service,
        "check_model_environment",
        lambda: ModelPreflightResult(
            ok=True,
            summary="模型配置已就绪",
            lines=("  ✓ models ready",),
            config_path="/tmp/config.yaml",
        ),
    )
    monkeypatch.setattr(
        service,
        "check_platform_environment",
        lambda _platform, **_options: SetupCheckResult(
            ok=True,
            summary="android 环境就绪",
            lines=("  ✓ adb connected", "  ⚠ scrcpy optional"),
        ),
    )

    result = service.check_environment("android")

    assert result.ok
    assert result.summary == "GUIWeave 运行环境已就绪"
    assert result.lines == (
        "  ✓ models ready",
        "  ✓ adb connected",
        "  ⚠ scrcpy optional",
    )


def test_run_blocks_before_building_platform_when_model_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ToolAgentService(log_root=tmp_path)
    monkeypatch.setattr(
        service,
        "check_model_environment",
        lambda: ModelPreflightResult(
            ok=False,
            summary="模型配置未就绪",
            lines=("  ✗ STANDARD_API_KEY 未配置",),
            config_path="/tmp/config.yaml",
        ),
    )
    monkeypatch.setattr(
        "gui_agent.core.tool_agent.service.build_platform",
        lambda *_args, **_kwargs: pytest.fail("platform must not be built"),
    )

    with pytest.raises(RuntimeError, match="STANDARD_API_KEY"):
        service.run("inspect account", platform="browser")


def test_run_honors_cancellation_before_model_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ToolAgentService(log_root=tmp_path / "logs")
    monkeypatch.setattr(
        service,
        "check_model_environment",
        lambda: pytest.fail("cancelled run reached model preflight"),
    )

    with pytest.raises(InterruptedError, match="before runtime startup"):
        service.run(
            "inspect account",
            platform="browser",
            stop_requested=lambda: True,
        )


def test_service_binds_roboteam_knowledge_from_current_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    knowledge_root = tmp_path / "knowledge"
    app_dir = knowledge_root / "browser" / "RoboTeam"
    app_dir.mkdir(parents=True)
    (app_dir / "_app.md").write_text(
        "---\nscope:\n  - orchestrator\n---\n"
        "# RoboTeam\n\nOrders are available from Orders > Order List.",
        encoding="utf-8",
    )
    (app_dir / "_deploy.md").write_text(
        "---\naliases:\n  - Robo Team\n---\n"
        "Entry URL: http://1.2.3.4:22000/",
        encoding="utf-8",
    )
    monkeypatch.setattr(app_summary, "KNOWLEDGE_DIR", knowledge_root)
    monkeypatch.setattr(
        app_summary,
        "get_user_knowledge_root",
        lambda: knowledge_root,
    )

    session = SimpleNamespace(client=SimpleNamespace(
        page_info=lambda: ("http://localhost:22000/orders/list", "Orders"),
        current_app_id=lambda: "",
    ))
    bundle = SimpleNamespace(
        platform="browser",
        setup_check=lambda: SetupCheckResult(ok=True, summary="ready", lines=()),
        make_status_reporter=lambda _show: None,
        open_session=lambda: nullcontext(session),
    )
    captured: dict = {}

    def fake_execute(**kwargs):
        captured.update(kwargs)
        return (
            SimpleNamespace(
                phase="completed",
                task_type="RETRIEVE",
                summary="done",
                output="[]",
            ),
            SimpleNamespace(reply="No orders"),
        )

    service = ToolAgentService(log_root=tmp_path / "logs")
    monkeypatch.setattr(
        service,
        "check_model_environment",
        lambda: ModelPreflightResult(
            ok=True,
            summary="ready",
            lines=(),
            config_path="/tmp/config.yaml",
        ),
    )
    monkeypatch.setattr("gui_agent.core.tool_agent.service.build_platform", lambda *_a, **_k: bundle)
    monkeypatch.setattr("gui_agent.core.tool_agent.service.execute_tool_agent", fake_execute)

    result = service.run(
        "查看当前站点的订单列表",
        platform="browser",
        show_hud=False,
    )

    assert result.phase == "completed"
    assert captured["app_router"]["active_app"] == "RoboTeam"
    assert "Orders > Order List" in captured["knowledge"]
    assert "http://1.2.3.4:22000/" in captured["access_context"]
    assert captured["knowledge_summary"]["app_name"] == "RoboTeam"
