from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from gui_agent.console import RunConsole, RunRequest, create_app
from gui_agent.core.config.preflight import ModelPreflightResult
from gui_agent.core.runtime.factory import SetupCheckResult
from gui_agent.core.tool_agent.service import ToolAgentService


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(ToolAgentService(log_root=tmp_path)))


def _run(tmp_path: Path) -> str:
    run_id = "tool_agent/browser/20260812_120000"
    run_dir = tmp_path / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "context.json").write_text(
        json.dumps(
            {
                "goal": "inspect orders",
                "platform": "browser",
                "outcome": {"phase": "completed", "summary": "done"},
                "models": {"tool_agent.master": "configured-model"},
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "tool_agent_trace.json").write_text(
        json.dumps(
            {
                "phase": "completed",
                "trace": [
                    {
                        "index": 1,
                        "layer": "runtime",
                        "event": "runtime_started",
                        "message": "started",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "report.html").write_text("<h1>report</h1>", encoding="utf-8")
    return run_id


def test_console_lists_runs_events_and_safe_artifacts(tmp_path: Path) -> None:
    run_id = _run(tmp_path)
    client = _client(tmp_path)

    listing = client.get("/api/runs")
    detail = client.get(f"/api/runs/{run_id}")
    events = client.get(f"/api/runs/{run_id}/events")
    report = client.get(f"/api/runs/{run_id}/artifacts/report")

    assert listing.status_code == 200
    assert listing.json()["runs"][0]["goal"] == "inspect orders"
    assert detail.json()["models"]["tool_agent.master"] == "configured-model"
    assert events.json()["events"][0]["event"] == "runtime_started"
    assert report.status_code == 200
    assert "report" in report.text


def test_console_rejects_path_traversal_and_unknown_artifact(tmp_path: Path) -> None:
    run_id = _run(tmp_path)
    client = _client(tmp_path)

    unknown = client.get(f"/api/runs/{run_id}/artifacts/private")
    traversal = client.get("/api/runs/%2E%2E/%2E%2E/private")

    assert unknown.status_code == 400
    assert traversal.status_code in {400, 404}


def test_console_home_explains_model_gateway_boundary(tmp_path: Path) -> None:
    response = _client(tmp_path).get("/")

    assert response.status_code == 200
    assert "模型网关" in response.text
    assert "API_KEY" in response.text
    assert "LOCAL RUNTIME" in response.text
    assert "platform-notice" in response.text
    assert "start-task" in response.text
    assert "结果 / 当前摘要" in response.text
    assert "NEW GUI RUN" in response.text
    assert "新建 GUI 任务" in response.text
    assert "Android 设备地址" in response.text
    assert 'name="adb_serial"' in response.text
    assert "新建 Tool Agent 任务" not in response.text


def test_console_frontend_auto_selects_and_prioritizes_final_reply() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "gui_agent"
        / "console_assets"
        / "console.js"
    ).read_text(encoding="utf-8")

    assert "state.active = state.runs[0]?.run_id || null" in source
    assert "detail.reply || detail.summary" in source
    assert "await selectRun(result.task_id)" not in source
    assert "state.active !== runId" in source
    assert 'class="event-frame frame-${frameLayout}"' in source
    assert 'loading="lazy"' in source
    assert 'String(event.frame_id || "").split(":").at(-1)' in source
    assert '["android", "iphone"].includes(run.platform) ? "portrait" : "wide"' in source
    assert 'data-frame-layout="${frameLayout}"' in source
    assert 'frameDialog.classList.toggle("frame-portrait"' in source
    assert 'action-event action-${actionState}' in source
    assert '✓ 执行成功' in source
    assert '! 未确认效果' in source
    assert 'query.set("adb_serial", androidAddress)' in source
    assert 'platform !== "android"' in source
    assert 'ANDROID_DEVICE_STORAGE_KEY = "guiweave.android.device"' in source
    assert "result.ok && platform === \"android\" && androidAddress" in source
    assert 'localStorage.setItem(ANDROID_DEVICE_STORAGE_KEY, address)' in source
    assert 'localStorage.removeItem(ANDROID_DEVICE_STORAGE_KEY)' in source
    assert "async function cancelTask()" in source
    assert "已经执行的 GUI 操作不会自动撤销" in source
    assert "中止中…" in source


def test_console_hidden_state_overrides_component_display_rules() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "gui_agent"
        / "console_assets"
        / "environment.css"
    ).read_text(encoding="utf-8")

    assert ".hidden" in source
    assert "display: none !important" in source
    assert ".event-frame.frame-wide img" in source
    assert ".event-frame.frame-portrait img" in source
    assert "height: 320px" in source
    assert "object-fit: contain" in source
    assert "dialog.frame-dialog.frame-portrait" in source
    assert "width: min(352px, calc(100% - 32px))" in source
    assert "background: #fff" in source


def test_console_projects_and_serves_referenced_event_screenshots(tmp_path: Path) -> None:
    run_id = "tool_agent/browser/20260812_120001"
    run_dir = tmp_path / run_id
    run_dir.mkdir(parents=True)
    frame = run_dir / "screenshot_tool_agent_1.png"
    frame.write_bytes(b"\x89PNG\r\n\x1a\n")
    (run_dir / "tool_agent_events.jsonl").write_text(
        json.dumps(
            {
                "index": 1,
                "event": "observe",
                "screenshot_path": str(frame),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    client = _client(tmp_path)

    events = client.get(f"/api/runs/{run_id}/events")
    image = client.get(
        "/api/run-frame",
        params={"run_id": run_id, "frame": frame.name},
    )
    report_relative_image = client.get(
        f"/api/runs/{run_id}/artifacts/{frame.name}"
    )

    assert events.status_code == 200
    assert events.json()["events"][0]["screenshot"] == {"name": frame.name}
    assert "screenshot_path" not in events.text
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"
    assert report_relative_image.status_code == 200
    assert report_relative_image.headers["content-type"] == "image/png"


def test_console_model_environment_does_not_return_secret(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = ToolAgentService(log_root=tmp_path)
    result = ModelPreflightResult(
        ok=False,
        summary="模型配置未就绪",
        lines=("  ✗ STANDARD_API_KEY 未配置",),
        config_path="/private/config.standard.yaml",
    )
    monkeypatch.setattr(service, "check_model_environment", lambda: result)

    response = TestClient(create_app(service)).get("/api/environment/model")

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert "secret-value" not in response.text


def test_console_platform_environment_passes_normalized_android_address(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = ToolAgentService(log_root=tmp_path)
    captured: dict[str, object] = {}

    def check(platform: str, **options: object) -> SetupCheckResult:
        captured.update(platform=platform, **options)
        return SetupCheckResult(
            ok=False,
            summary=f"{platform} dependency missing",
            lines=("  ✗ adb unavailable",),
        )

    monkeypatch.setattr(
        service,
        "check_platform_environment",
        check,
    )

    response = TestClient(create_app(service)).get(
        "/api/environment/android",
        params={"adb_serial": " 192.168.1.50 "},
    )

    assert response.status_code == 200
    assert response.json() == {
        "ok": False,
        "summary": "android dependency missing",
        "details": ["  ✗ adb unavailable"],
    }
    assert captured == {"platform": "android", "serial": "192.168.1.50:5555"}


def test_console_run_request_normalizes_android_address() -> None:
    request = RunRequest(
        goal="打开设置",
        platform="android",
        adb_serial=" 192.168.1.88 ",
    )

    assert request.adb_serial == "192.168.1.88:5555"
    assert RunRequest(goal="打开设置", platform="android", adb_serial=" ").adb_serial is None
    assert RunRequest(
        goal="打开设置",
        platform="android",
        adb_serial="emulator-5554",
    ).adb_serial == "emulator-5554"


class _BlockingService:
    def __init__(self, log_root: Path) -> None:
        self.log_root = log_root
        self.started = threading.Event()

    def check_environment(self, _platform: str, **_options: object):
        return SetupCheckResult(ok=True, summary="ready")

    def run(self, _goal: str, **options: object):
        callback = options["on_run_created"]
        stop_requested = options["stop_requested"]
        run_id = "tool_agent/browser/20260812_130000"
        callback(run_id, self.log_root / run_id)  # type: ignore[operator]
        self.started.set()
        while not stop_requested():  # type: ignore[operator]
            time.sleep(0.005)
        return SimpleNamespace(
            run_id=run_id,
            phase="failed",
            to_dict=lambda: {"run_id": run_id, "phase": "failed"},
        )


class _NoDuplicatePreflightService:
    def __init__(self, log_root: Path) -> None:
        self.log_root = log_root

    def check_environment(self, _platform: str, **_options: object):
        raise AssertionError("RunConsole.submit repeated the platform preflight")

    def run(self, _goal: str, **options: object):
        run_id = "tool_agent/browser/20260812_140000"
        options["on_run_created"](run_id, self.log_root / run_id)  # type: ignore[operator]
        return SimpleNamespace(
            run_id=run_id,
            phase="completed",
            to_dict=lambda: {"run_id": run_id, "phase": "completed"},
        )


class _NeverRunService:
    def run(self, _goal: str, **_options: object):
        raise AssertionError("a queued cancelled task must not start")


def test_console_cancels_queued_task_before_service_start(
    monkeypatch,
) -> None:
    threads: list[threading.Thread] = []
    monkeypatch.setattr(threading.Thread, "start", lambda thread: threads.append(thread))
    console = RunConsole(_NeverRunService())  # type: ignore[arg-type]

    task = console.submit(RunRequest(goal="inspect account", platform="browser"))
    console.cancel(task.task_id)
    assert task.status == "cancelling"
    threads[0].run()

    assert task.status == "interrupted"


def test_console_submit_does_not_repeat_platform_preflight(tmp_path: Path) -> None:
    client = TestClient(create_app(_NoDuplicatePreflightService(tmp_path)))  # type: ignore[arg-type]

    response = client.post(
        "/api/tasks",
        json={
            "goal": "inspect example",
            "platform": "browser",
            "headless": True,
        },
    )

    assert response.status_code == 202


def test_console_promotes_run_id_blocks_platform_conflict_and_cancels(
    tmp_path: Path,
) -> None:
    service = _BlockingService(tmp_path)
    console = RunConsole(service)  # type: ignore[arg-type]
    request = RunRequest(goal="inspect account", platform="browser")

    task = console.submit(request)
    assert service.started.wait(timeout=1)
    assert task.run_id == "tool_agent/browser/20260812_130000"

    try:
        console.submit(request)
    except ValueError as exc:
        assert "already has an active task" in str(exc)
    else:  # pragma: no cover - protects the resource ownership invariant
        raise AssertionError("a second browser task should have been rejected")

    console.cancel(task.task_id)
    deadline = time.monotonic() + 1
    while task.status == "cancelling" and time.monotonic() < deadline:
        time.sleep(0.005)
    assert task.status == "interrupted"


def test_console_cancel_endpoint_interrupts_active_task(tmp_path: Path) -> None:
    service = _BlockingService(tmp_path)
    with TestClient(create_app(service)) as client:  # type: ignore[arg-type]
        created = client.post(
            "/api/tasks",
            json={"goal": "inspect account", "platform": "browser"},
        )
        assert created.status_code == 202
        assert service.started.wait(timeout=1)

        task_id = created.json()["task_id"]
        cancelled = client.post(f"/api/tasks/{task_id}/cancel")

        assert cancelled.status_code == 202
        assert cancelled.json() == {"task_id": task_id, "status": "cancelling"}
        deadline = time.monotonic() + 1
        task_status = "cancelling"
        while task_status == "cancelling" and time.monotonic() < deadline:
            tasks = client.get("/api/tasks").json()["tasks"]
            task_status = next(
                item["status"] for item in tasks if item["task_id"] == task_id
            )
            time.sleep(0.005)
        assert task_status == "interrupted"
