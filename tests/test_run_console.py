from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from gui_agent.adapters.browser.factory import DEFAULT_BROWSER_START_URL
from gui_agent.console import ChatRequest, RunConsole, RunRequest, create_app
from gui_agent.core.chat_router import ChatRoute
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
    assert "MISSION INDEX" in response.text
    assert "SYSTEM STANDBY" in response.text
    assert "RUN TELEMETRY" in response.text
    assert "platform-notice" in response.text
    assert "start-task" in response.text
    assert "结果 / 当前摘要" in response.text
    assert "运行指标" in response.text
    assert 'id="sidebar-toggle"' in response.text
    assert 'id="trace-follow"' in response.text
    assert 'id="previous-frame"' in response.text
    assert 'data-console-mode="chat"' in response.text
    assert 'id="chat-form"' in response.text
    assert 'id="chat-thread"' in response.text
    assert 'id="chat-detail"' in response.text
    assert 'id="chat-new-session"' in response.text
    assert "只有必须读取或操作界面时" in response.text
    assert "NEW GUI RUN" in response.text
    assert "新建 GUI 任务" in response.text
    assert "Android 设备地址" in response.text
    assert 'name="adb_serial"' in response.text
    assert "Browser 起始页" in response.text
    assert 'name="start_url"' in response.text
    assert f'value="{DEFAULT_BROWSER_START_URL}"' in response.text
    assert "新建 Tool Agent 任务" not in response.text


def test_console_rejects_cross_origin_mutations(tmp_path: Path) -> None:
    client = _client(tmp_path)

    assert client.post(
        "/api/chat/session",
        headers={"origin": "https://example.com"},
    ).status_code == 403
    assert client.post(
        "/api/chat/session",
        headers={"origin": "http://127.0.0.1:7468"},
    ).status_code == 201


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
    assert 'action-event action-${eventActionState}' in source
    assert '✓ 执行成功' in source
    assert '! 未确认效果' in source
    assert 'state.eventFilter = button.dataset.eventFilter' in source
    assert 'previousScroll + list.scrollHeight - previousHeight' in source
    assert 'showFrame(state.frameIndex + (event.key === "ArrowLeft" ? -1 : 1))' in source
    assert 'frame.name === state.frameName' in source
    assert 'document.body.classList.toggle("sidebar-collapsed")' in source
    assert 'request("/api/chat")' in source
    assert 'request("/api/chat/messages"' in source
    assert 'request("/api/chat/session"' in source
    assert 'state.chatTurn = null' in source
    assert "已有 Runs 不会被删除" in source
    assert 'class="chat-run-meta"' in source
    assert 'data-chat-turn="${escapeHtml(turn.turn_id)}"' in source
    assert '<div class="chat-message user"><small>YOU</small>' in source
    assert 'class="chat-route ${escapeHtml(turn.route)}"' in source
    assert '<small>${routeLabel[turn.route]' not in source
    assert 'clarify: "需要补充"' in source
    assert "renderChatDetail(turns.find" in source
    assert "state.chatTurn = response.turn.turn_id" in source
    assert '$("runs-mode").classList.toggle("hidden", chat)' in source
    assert 'if (chat) {\n    updateChatPlatform();' in source
    assert "function loadChatEnvironment" not in source
    assert 'event.key === "Enter" && !event.shiftKey && !event.isComposing' in source
    assert 'query.set("adb_serial", androidAddress)' in source
    assert 'platform !== "android"' in source
    assert 'ANDROID_DEVICE_STORAGE_KEY = "guiweave.android.device"' in source
    assert "result.ok && platform === \"android\" && androidAddress" in source
    assert 'localStorage.setItem(ANDROID_DEVICE_STORAGE_KEY, address)' in source
    assert 'localStorage.removeItem(ANDROID_DEVICE_STORAGE_KEY)' in source
    assert "const CONSOLE_HEADLESS = true" in source
    assert f'const DEFAULT_BROWSER_START_URL = "{DEFAULT_BROWSER_START_URL}"' in source
    assert '$("chat-browser-field").classList.toggle("hidden", platform !== "browser")' in source
    assert '$("browser-start-url-field").classList.toggle("hidden", platform !== "browser")' in source
    assert "start_url: startUrl" in source
    assert 'data.start_url = data.platform === "browser"' in source
    assert 'query.set("headless", String(CONSOLE_HEADLESS))' in source
    assert "headless: CONSOLE_HEADLESS" in source
    assert "async function cancelTask()" in source
    assert "已经执行的 GUI 操作不会自动撤销" in source
    assert "中止中…" in source


def test_console_hidden_state_overrides_component_display_rules() -> None:
    console_source = (
        Path(__file__).resolve().parents[1]
        / "gui_agent"
        / "console_assets"
        / "console.css"
    ).read_text(encoding="utf-8")
    source = (
        Path(__file__).resolve().parents[1]
        / "gui_agent"
        / "console_assets"
        / "environment.css"
    ).read_text(encoding="utf-8")

    assert "color-scheme: dark" in console_source
    assert "--cyan: #59e3ff" in console_source
    assert "--faint: #718198" in console_source
    assert ".chat-shell" in console_source
    assert ".chat-detail" in console_source
    assert "height: calc(100vh - 74px)" in console_source
    assert "font-size: 15px" in console_source
    assert "font-size: 14px; line-height: 1.65" in console_source
    assert "font: 500 14px/1.55 var(--sans)" in console_source
    assert "font: 800 8px" not in console_source
    assert "font: 800 8px" not in source
    assert "@media (max-width: 760px)" in console_source
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
    captured.clear()
    TestClient(create_app(service)).get("/api/environment/browser")
    assert captured == {"platform": "browser", "cdp_url": None, "headless": True}


def test_console_submit_enforces_background_only_options(monkeypatch) -> None:
    monkeypatch.setattr(threading.Thread, "start", lambda _thread: None)
    console = RunConsole(_NeverRunService())  # type: ignore[arg-type]
    assert RunRequest(goal="inspect", platform="browser").headless is True
    assert RunRequest(goal="inspect", platform="browser").start_url == DEFAULT_BROWSER_START_URL
    assert RunRequest(goal="inspect", platform="browser", start_url=" ").start_url == DEFAULT_BROWSER_START_URL
    task = console.submit(RunRequest(
        goal="inspect", platform="browser", mode="chat", headless=False, show_hud=True,
    ))

    assert task.request.headless is True
    assert task.request.show_hud is False
    assert task.request.mode == "chat"
    assert console.tasks()[0]["mode"] == "chat"


def test_console_production_browser_preserves_headed_cdp(monkeypatch) -> None:
    monkeypatch.setattr(threading.Thread, "start", lambda _thread: None)
    console = RunConsole(_NeverRunService())  # type: ignore[arg-type]
    task = console.submit(RunRequest(
        goal="inspect",
        platform="browser",
        browser_profile="production",
    ))

    assert task.request.headless is False
    assert task.request.show_hud is False


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


class _ChatRouter:
    def __init__(self, *routes: ChatRoute) -> None:
        self.routes = list(routes)
        self.calls = []

    def route(self, message, history, platform):
        self.calls.append((message, history, platform))
        return self.routes.pop(0)


class _ImmediateChatService:
    def __init__(self, log_root: Path) -> None:
        self.log_root = log_root
        self.goals = []
        self.options = []

    def run(self, goal: str, **options: object):
        self.goals.append(goal)
        self.options.append(options)
        run_id = "tool_agent/browser/20260813_190000"
        options["on_run_created"](run_id, self.log_root / run_id)  # type: ignore[operator]
        return SimpleNamespace(
            run_id=run_id,
            phase="completed",
            to_dict=lambda: {
                "run_id": run_id,
                "phase": "completed",
                "summary": "page opened",
                "reply": "页面已经打开。",
            },
        )


def test_console_passes_browser_start_url_to_service(tmp_path: Path) -> None:
    service = _ImmediateChatService(tmp_path)
    console = RunConsole(service)  # type: ignore[arg-type]

    task = console.submit(RunRequest(
        goal="inspect",
        platform="browser",
        start_url="https://example.com/start",
    ))
    deadline = time.monotonic() + 1
    while task.status in {"queued", "running"} and time.monotonic() < deadline:
        time.sleep(0.005)

    assert task.status == "completed"
    assert service.options[0]["start_url"] == "https://example.com/start"


def test_console_chat_responds_without_starting_gui() -> None:
    router = _ChatRouter(ChatRoute(
        route="respond",
        reply="你好，我可以帮你判断是否需要操作界面。",
        reason="greeting needs no GUI evidence",
    ))
    client = TestClient(create_app(_NeverRunService(), router))  # type: ignore[arg-type]

    response = client.post(
        "/api/chat/messages",
        json={"message": "你好", "platform": "browser"},
    )

    assert response.status_code == 200
    turn = response.json()["turn"]
    assert turn["route"] == "respond"
    assert turn["task_id"] is None
    assert client.get("/api/tasks").json() == {"tasks": []}
    assert client.get("/api/chat").json()["turns"] == [turn]


def test_console_starts_fresh_chat_session_without_deleting_runs(tmp_path: Path) -> None:
    router = _ChatRouter(ChatRoute(
        route="respond",
        reply="你好。",
        reason="ordinary conversation",
    ))
    console = RunConsole(_ImmediateChatService(tmp_path), router)  # type: ignore[arg-type]
    task = console.submit(RunRequest(goal="inspect", platform="browser"))
    deadline = time.monotonic() + 1
    while task.status in {"queued", "running"} and time.monotonic() < deadline:
        time.sleep(0.005)
    old_chat_id = console.chat()["chat_id"]
    console.post_chat(ChatRequest(message="你好", platform="browser"))

    fresh = console.new_chat_session()

    assert fresh == console.chat()
    assert fresh["chat_id"] != old_chat_id
    assert fresh["turns"] == []
    assert console.tasks()[0]["task_id"] == task.task_id
    assert console.tasks()[0]["status"] == "completed"


def test_console_rejects_new_chat_session_while_gui_task_is_active(
    tmp_path: Path,
) -> None:
    router = _ChatRouter(ChatRoute(
        route="gui",
        gui_goal="Open the requested page.",
        reason="current browser state is required",
    ))
    service = _BlockingService(tmp_path)
    client = TestClient(create_app(service, router))  # type: ignore[arg-type]
    turn = client.post(
        "/api/chat/messages",
        json={"message": "打开页面", "platform": "browser"},
    ).json()["turn"]
    assert service.started.wait(timeout=1)

    response = client.post("/api/chat/session")

    assert response.status_code == 400
    assert "Chat GUI 任务未结束" in response.json()["detail"]
    client.post(f"/api/tasks/{turn['task_id']}/cancel")


def test_console_chat_history_retains_active_gui_turn(tmp_path: Path) -> None:
    router = _ChatRouter(
        ChatRoute(
            route="gui",
            gui_goal="Open the requested page.",
            reason="current browser state is required",
        ),
        *[
            ChatRoute(route="respond", reply=f"reply {index}", reason="chat")
            for index in range(105)
        ],
    )
    service = _BlockingService(tmp_path)
    console = RunConsole(service, router)  # type: ignore[arg-type]
    gui_turn = console.post_chat(ChatRequest(message="打开页面", platform="browser"))
    assert service.started.wait(timeout=1)

    for index in range(105):
        console.post_chat(ChatRequest(message=f"message {index}", platform="browser"))

    turns = console.chat()["turns"]
    assert len(turns) == 100
    assert gui_turn.turn_id in {turn["turn_id"] for turn in turns}
    console.cancel(gui_turn.task_id or "")


def test_console_router_history_includes_older_active_gui_turn(tmp_path: Path) -> None:
    router = _ChatRouter(
        ChatRoute(
            route="gui",
            gui_goal="Open the requested page.",
            reason="current browser state is required",
        ),
        *[
            ChatRoute(route="respond", reply=f"reply {index}", reason="chat")
            for index in range(13)
        ],
        ChatRoute(route="cancel", reply="停止任务。", reason="stop active task"),
    )
    service = _BlockingService(tmp_path)
    console = RunConsole(service, router)  # type: ignore[arg-type]
    gui_turn = console.post_chat(ChatRequest(message="打开页面", platform="browser"))
    assert service.started.wait(timeout=1)
    for index in range(13):
        console.post_chat(ChatRequest(message=f"message {index}", platform="browser"))

    console.post_chat(ChatRequest(message="停止任务", platform="browser"))

    history = router.calls[-1][1]
    assert len(history) == 12
    assert any(turn["task_id"] == gui_turn.task_id for turn in history)
    deadline = time.monotonic() + 1
    while console.tasks()[0]["status"] == "cancelling" and time.monotonic() < deadline:
        time.sleep(0.005)
    assert console.tasks()[0]["status"] == "interrupted"


def test_console_chat_routes_gui_result_back_into_next_turn(tmp_path: Path) -> None:
    router = _ChatRouter(
        ChatRoute(
            route="gui",
            gui_goal="Open the Google homepage.",
            reason="current browser state is required",
        ),
        ChatRoute(
            route="respond",
            reply="上一步已经成功打开页面。",
            reason="the public GUI result answers the question",
        ),
    )
    service = _ImmediateChatService(tmp_path)
    client = TestClient(create_app(service, router))  # type: ignore[arg-type]

    first = client.post(
        "/api/chat/messages",
        json={"message": "打开 Google", "platform": "browser"},
    )
    assert first.status_code == 200
    deadline = time.monotonic() + 1
    gui_turn = first.json()["turn"]
    while gui_turn["status"] in {"queued", "running"} and time.monotonic() < deadline:
        gui_turn = client.get("/api/chat").json()["turns"][0]
        time.sleep(0.005)

    second = client.post(
        "/api/chat/messages",
        json={"message": "完成了吗？", "platform": "browser"},
    )

    assert gui_turn["status"] == "completed"
    assert gui_turn["assistant"] == "页面已经打开。"
    assert gui_turn["task_id"].startswith("task_")
    assert service.goals == ["Open the Google homepage."]
    assert second.json()["turn"]["route"] == "respond"
    assert router.calls[1][1][-1]["assistant"] == "页面已经打开。"
    assert len(client.get("/api/chat").json()["turns"]) == 2


def test_console_chat_cancel_route_stops_active_gui_task(tmp_path: Path) -> None:
    router = _ChatRouter(
        ChatRoute(
            route="gui",
            gui_goal="Open the requested page.",
            reason="current browser state is required",
        ),
        ChatRoute(
            route="cancel",
            reply="好的，停止当前任务。",
            reason="user asked to stop the active GUI task",
        ),
    )
    service = _BlockingService(tmp_path)
    client = TestClient(create_app(service, router))  # type: ignore[arg-type]

    first = client.post(
        "/api/chat/messages",
        json={"message": "打开页面", "platform": "browser"},
    ).json()["turn"]
    assert service.started.wait(timeout=1)

    cancelled = client.post(
        "/api/chat/messages",
        json={"message": "算了，停止操作", "platform": "browser"},
    ).json()["turn"]

    assert cancelled["route"] == "cancel"
    assert cancelled["task_id"] == first["task_id"]
    assert cancelled["assistant"] == "已请求中止当前 GUI 任务。"
    deadline = time.monotonic() + 1
    gui_turn = client.get("/api/chat").json()["turns"][0]
    while gui_turn["status"] == "cancelling" and time.monotonic() < deadline:
        time.sleep(0.005)
        gui_turn = client.get("/api/chat").json()["turns"][0]
    assert gui_turn["status"] == "interrupted"
    assert gui_turn["assistant"] == "GUI 任务已中止。"


def test_console_chat_cancel_targets_task_id_not_selected_platform(tmp_path: Path) -> None:
    router = _ChatRouter(
        ChatRoute(route="gui", gui_goal="Open page.", reason="browser task"),
        ChatRoute(route="gui", gui_goal="Open settings.", reason="android task"),
    )
    service = _BlockingService(tmp_path)
    console = RunConsole(service, router)  # type: ignore[arg-type]
    console.post_chat(ChatRequest(message="打开页面", platform="browser"))
    console.post_chat(ChatRequest(message="打开设置", platform="android"))
    deadline = time.monotonic() + 1
    tasks = console.tasks()
    while (
        (len(tasks) < 2 or any(task["status"] != "running" for task in tasks))
        and time.monotonic() < deadline
    ):
        time.sleep(0.005)
        tasks = console.tasks()
    assert len(tasks) == 2
    assert all(task["status"] == "running" for task in tasks)
    android = next(task for task in tasks if task["platform"] == "android")
    browser = next(task for task in tasks if task["platform"] == "browser")
    router.routes.append(ChatRoute(
        route="cancel",
        reply="停止 Android 任务。",
        cancel_task_id=android["task_id"],
        reason="user selected the Android task",
    ))

    turn = console.post_chat(ChatRequest(
        message="停止 Android 任务",
        platform="browser",
    ))

    assert turn.task_id == android["task_id"]
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        tasks = console.tasks()
        statuses = {task["task_id"]: task["status"] for task in tasks}
        if statuses[android["task_id"]] == "interrupted":
            break
        time.sleep(0.005)
    assert statuses[android["task_id"]] == "interrupted"
    assert statuses[browser["task_id"]] == "running"
    console.cancel(browser["task_id"])


def test_console_chat_cancel_without_active_task_is_honest() -> None:
    router = _ChatRouter(ChatRoute(
        route="cancel",
        reply="好的，停止当前任务。",
        reason="user requested cancellation",
    ))
    client = TestClient(create_app(_NeverRunService(), router))  # type: ignore[arg-type]

    turn = client.post(
        "/api/chat/messages",
        json={"message": "停止任务", "platform": "browser"},
    ).json()["turn"]

    assert turn["route"] == "respond"
    assert turn["assistant"] == "当前没有正在执行的 GUI 任务。"
    assert turn["task_id"] is None


def test_console_chat_cancel_does_not_stop_run_mode_task(monkeypatch) -> None:
    threads: list[threading.Thread] = []
    monkeypatch.setattr(threading.Thread, "start", lambda thread: threads.append(thread))
    router = _ChatRouter(ChatRoute(
        route="cancel",
        reply="好的，停止当前任务。",
        reason="user requested cancellation",
    ))
    console = RunConsole(_NeverRunService(), router)  # type: ignore[arg-type]
    run_task = console.submit(RunRequest(goal="inspect", platform="browser"))

    turn = console.post_chat(ChatRequest(message="停止任务", platform="browser"))

    assert turn.route == "respond"
    assert turn.assistant == "当前没有正在执行的 GUI 任务。"
    assert run_task.status == "queued"


def test_console_chat_router_failure_does_not_start_gui() -> None:
    router = SimpleNamespace(
        route=lambda *_args: (_ for _ in ()).throw(RuntimeError("gateway unavailable"))
    )
    client = TestClient(create_app(_NeverRunService(), router))  # type: ignore[arg-type]

    response = client.post(
        "/api/chat/messages",
        json={"message": "处理一下", "platform": "browser"},
    )

    turn = response.json()["turn"]
    assert turn["route"] == "clarify"
    assert turn["status"] == "waiting"
    assert turn["task_id"] is None


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


def test_console_thread_start_failure_does_not_block_platform(monkeypatch) -> None:
    attempts = 0

    def start(_thread):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("thread unavailable")

    monkeypatch.setattr(threading.Thread, "start", start)
    console = RunConsole(_NeverRunService())  # type: ignore[arg-type]
    request = RunRequest(goal="inspect", platform="browser")

    with pytest.raises(RuntimeError, match="thread unavailable"):
        console.submit(request)

    assert console.tasks()[0]["status"] == "failed"
    assert console.submit(request).status == "queued"


def test_console_marks_result_serialization_failure_as_failed(monkeypatch) -> None:
    monkeypatch.setattr(threading.Thread, "start", lambda thread: thread.run())
    result = SimpleNamespace(
        run_id="tool_agent/browser/broken",
        phase="completed",
        to_dict=lambda: (_ for _ in ()).throw(ValueError("invalid result")),
    )
    service = SimpleNamespace(run=lambda *_args, **_kwargs: result)
    console = RunConsole(service)  # type: ignore[arg-type]

    task = console.submit(RunRequest(goal="inspect", platform="browser"))

    assert task.status == "failed"
    assert task.error == "invalid result"


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
