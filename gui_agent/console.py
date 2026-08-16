"""Local-only Run Console for supervising GUIWeave Tool Agent tasks."""

from __future__ import annotations

import mimetypes
import re
import secrets
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator

from gui_agent.adapters.browser.factory import DEFAULT_BROWSER_START_URL
from gui_agent.core.chat_router import ChatIntentRouter, ChatRoute, ChatRouteName
from gui_agent.core.tool_agent.service import ToolAgentService
from gui_agent.core.runtime.platforms import PlatformName


ASSET_ROOT = Path(__file__).resolve().parent / "console_assets"
ACTIVE_TASK_STATUSES = {"queued", "running", "cancelling"}
ROUTER_HISTORY_LIMIT = 12
CHAT_HISTORY_LIMIT = 100
LOCAL_ORIGIN = re.compile(
    r"https?://(?:localhost|127\.0\.0\.1|\[::1\])(?::\d+)?$"
)


def _normalize_android_serial(value: str | None) -> str | None:
    """Accept a bare device IP while preserving ordinary adb serials."""

    serial = (value or "").strip()
    if not serial:
        return None
    if re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", serial):
        return f"{serial}:5555"
    return serial


class _ExecutionOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: PlatformName
    perception: Literal["vision-only", "enhanced"] = "enhanced"
    max_turns: int = Field(default=50, ge=1, le=50)
    cdp_url: str | None = Field(default=None, max_length=500)
    start_url: str = Field(default=DEFAULT_BROWSER_START_URL, max_length=2048)
    adb_serial: str | None = Field(default=None, max_length=200)
    multi_action: bool = True

    @field_validator("adb_serial", mode="before")
    @classmethod
    def normalize_adb_serial(cls, value: object) -> str | None:
        return _normalize_android_serial(None if value is None else str(value))

    @field_validator("start_url", mode="before")
    @classmethod
    def normalize_start_url(cls, value: object) -> str:
        return str(value or "").strip() or DEFAULT_BROWSER_START_URL


class RunRequest(_ExecutionOptions):
    goal: str = Field(min_length=1, max_length=4000)
    mode: Literal["run", "chat"] = "run"
    headless: bool = True
    show_hud: bool = False


class ChatRequest(_ExecutionOptions):
    message: str = Field(min_length=1, max_length=4000)

    @field_validator("message")
    @classmethod
    def normalize_message(cls, value: str) -> str:
        message = value.strip()
        if not message:
            raise ValueError("message must not be empty")
        return message

    def to_run_request(self, goal: str) -> RunRequest:
        return RunRequest(goal=goal, mode="chat", **self.model_dump(exclude={"message"}))


@dataclass
class ChatTurn:
    turn_id: str
    user: str
    assistant: str
    route: ChatRouteName
    reason: str
    platform: PlatformName
    status: str
    created_at: str
    gui_goal: str = ""
    task_id: str | None = None
    run_id: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None


@dataclass
class ActiveTask:
    task_id: str
    request: RunRequest
    status: str = "queued"
    run_id: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    chat_turn: ChatTurn | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)


class RunConsole:
    """Keep only ephemeral process handles; durable task state lives in run artifacts."""

    def __init__(
        self,
        service: ToolAgentService | None = None,
        chat_router: ChatIntentRouter | None = None,
    ) -> None:
        self.service = service or ToolAgentService()
        self._tasks: dict[str, ActiveTask] = {}
        self._chat_id = f"chat_{secrets.token_hex(6)}"
        self._chat_turns: list[ChatTurn] = []
        self._chat_router = chat_router
        self._chat_gate = threading.Lock()
        self._lock = threading.Lock()

    def submit(
        self,
        request: RunRequest,
        *,
        chat_turn: ChatTurn | None = None,
    ) -> ActiveTask:
        # Reject stale page options at the server boundary: Console is background-only.
        request = request.model_copy(update={"headless": True, "show_hud": False})
        task = ActiveTask(
            task_id=f"task_{secrets.token_hex(6)}",
            request=request,
            chat_turn=chat_turn,
        )
        with self._lock:
            active = next(
                (
                    item
                    for item in self._tasks.values()
                    if item.request.platform == request.platform
                    and item.status in ACTIVE_TASK_STATUSES
                ),
                None,
            )
            if active is not None:
                raise ValueError(
                    f"{request.platform} already has an active task: {active.task_id}"
                )
            self._tasks[task.task_id] = task
            self._sync_chat_turn_locked(task)
        thread = threading.Thread(
            target=self._run_task,
            args=(task,),
            name=f"guiweave-console-{task.task_id}",
            daemon=True,
        )
        try:
            thread.start()
        except Exception as exc:
            with self._lock:
                task.status, task.error = "failed", str(exc)
                self._sync_chat_turn_locked(task)
            raise
        return task

    def _run_task(self, task: ActiveTask) -> None:
        with self._lock:
            if task.cancel_event.is_set():
                task.status = "interrupted"
                self._sync_chat_turn_locked(task)
                return
            task.status = "running"
            self._sync_chat_turn_locked(task)
        request = task.request
        options: dict[str, object]
        if request.platform == "browser":
            options = {
                "cdp_url": request.cdp_url,
                "start_url": request.start_url,
                "headless": request.headless,
            }
        elif request.platform == "android":
            options = {"serial": request.adb_serial}
        else:
            options = {}

        def record_run(run_id: str, _run_dir: Path) -> None:
            with self._lock:
                task.run_id = run_id
                self._sync_chat_turn_locked(task)

        try:
            result = self.service.run(
                request.goal,
                platform=request.platform,
                perception_mode=request.perception,
                max_turns=request.max_turns,
                allow_multi_action=request.multi_action,
                show_hud=request.show_hud and not request.headless,
                mirror_stdio=False,
                stop_requested=task.cancel_event.is_set,
                on_run_created=record_run,
                **options,
            )
            result_payload = result.to_dict()
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                task.status = "interrupted" if task.cancel_event.is_set() else "failed"
                task.error = str(exc)
                self._sync_chat_turn_locked(task)
            return
        with self._lock:
            task.run_id = result.run_id
            task.result = result_payload
            task.status = (
                "interrupted"
                if task.cancel_event.is_set()
                else "completed" if result.phase == "completed" else "failed"
            )
            self._sync_chat_turn_locked(task)

    def cancel(self, task_id: str) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise FileNotFoundError(f"unknown active task: {task_id}")
            if task.status not in ACTIVE_TASK_STATUSES:
                raise ValueError(f"task is already {task.status}")
            task.cancel_event.set()
            task.status = "cancelling"
            self._sync_chat_turn_locked(task)

    def chat(self) -> dict[str, Any]:
        with self._lock:
            return {
                "chat_id": self._chat_id,
                "turns": [asdict(turn) for turn in self._chat_turns],
            }

    def _active_chat_tasks_locked(self) -> list[ActiveTask]:
        return [
            task
            for task in self._tasks.values()
            if task.chat_turn and task.status in ACTIVE_TASK_STATUSES
        ]

    def new_chat_session(self) -> dict[str, Any]:
        with self._chat_gate, self._lock:
            active = next(iter(self._active_chat_tasks_locked()), None)
            if active:
                raise ValueError(
                    f"当前仍有 Chat GUI 任务未结束: {active.task_id} ({active.status})"
                )
            self._chat_id = f"chat_{secrets.token_hex(6)}"
            self._chat_turns.clear()
            return {"chat_id": self._chat_id, "turns": []}

    def post_chat(self, request: ChatRequest) -> ChatTurn:
        with self._chat_gate:
            with self._lock:
                recent = self._chat_turns[-ROUTER_HISTORY_LIMIT:]
                recent_ids = {turn.turn_id for turn in recent}
                missing = [
                    task.chat_turn
                    for task in self._active_chat_tasks_locked()
                    if task.chat_turn.turn_id not in recent_ids
                ]
                if missing:
                    tail = max(0, ROUTER_HISTORY_LIMIT - len(missing))
                    recent = missing + (self._chat_turns[-tail:] if tail else [])
                history = [self._chat_history_item(turn) for turn in recent]
            try:
                router = self._chat_router or ChatIntentRouter()
                self._chat_router = router
                route = router.route(request.message, history, request.platform)
            except Exception as exc:  # noqa: BLE001 - routing fails closed without GUI
                route = ChatRoute(
                    route="clarify",
                    reply="我暂时无法判断这条消息是否需要操作界面，请明确说明希望我查看或执行什么。",
                    reason=f"router unavailable: {type(exc).__name__}",
                )
            status = {"gui": "queued", "clarify": "waiting"}.get(
                route.route, "completed"
            )
            turn = ChatTurn(
                turn_id=f"turn_{secrets.token_hex(6)}",
                user=request.message,
                assistant=(
                    "已识别为 GUI 任务，正在后台执行…"
                    if route.route == "gui"
                    else route.reply.strip()
                ),
                route=route.route,
                reason=route.reason,
                platform=request.platform,
                status=status,
                created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                gui_goal=route.gui_goal.strip(),
            )
            with self._lock:
                self._chat_turns.append(turn)
                if len(self._chat_turns) > CHAT_HISTORY_LIMIT:
                    active_ids = {
                        task.chat_turn.turn_id
                        for task in self._active_chat_tasks_locked()
                    }
                    drop = next(
                        index
                        for index, item in enumerate(self._chat_turns)
                        if item.turn_id not in active_ids
                    )
                    del self._chat_turns[drop]
            if route.route == "cancel":
                self._cancel_from_chat(turn, route.cancel_task_id)
            elif route.route == "gui":
                try:
                    self.submit(
                        request.to_run_request(route.gui_goal),
                        chat_turn=turn,
                    )
                except Exception as exc:  # noqa: BLE001 - becomes a visible chat result
                    with self._lock:
                        turn.status = "failed"
                        turn.error = str(exc)
                        turn.assistant = f"GUI 任务未能启动：{exc}"
            return turn

    def _cancel_from_chat(self, turn: ChatTurn, task_id: str) -> None:
        with self._lock:
            active = self._active_chat_tasks_locked()
            target = (
                next((task for task in active if task.task_id == task_id), None)
                if task_id
                else active[0] if len(active) == 1 else None
            )
            if target is None:
                turn.route = "clarify" if active else "respond"
                turn.status = "waiting" if active else "completed"
                turn.assistant = (
                    "未找到与取消请求匹配的活动 GUI 任务，请明确要中止的任务。"
                    if active and task_id
                    else "当前有多个正在执行的 GUI 任务，请明确要中止的任务。"
                    if active else "当前没有正在执行的 GUI 任务。"
                )
                turn.reason = "取消目标不明确。" if active else "当前没有活动的 GUI 任务。"
                return
            task_id = target.task_id
            already_cancelling = target.status == "cancelling"
        try:
            self.cancel(task_id)
        except ValueError:
            with self._lock:
                turn.route = "respond"
                turn.assistant = "该 GUI 任务已经结束。"
            return
        with self._lock:
            turn.task_id = target.task_id
            turn.run_id = target.run_id
            turn.assistant = (
                "当前 GUI 任务正在中止。"
                if already_cancelling
                else "已请求中止当前 GUI 任务。"
            )

    @staticmethod
    def _chat_history_item(turn: ChatTurn) -> dict[str, Any]:
        return {
            "user": turn.user,
            "assistant": turn.assistant,
            "route": turn.route,
            "status": turn.status,
            "gui_goal": turn.gui_goal,
            "task_id": turn.task_id,
            "run_id": turn.run_id,
        }

    def _sync_chat_turn_locked(self, task: ActiveTask) -> None:
        turn = task.chat_turn
        if turn is None:
            return
        turn.task_id = task.task_id
        turn.run_id = task.run_id
        turn.status = task.status
        turn.result = task.result
        turn.error = task.error
        if task.status == "interrupted":
            turn.assistant = "GUI 任务已中止。"
        elif task.result:
            turn.assistant = str(
                task.result.get("reply")
                or task.result.get("summary")
                or "GUI 任务已结束。"
            )
        elif task.error:
            turn.assistant = f"GUI 任务失败：{task.error}"

    def tasks(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "task_id": task.task_id,
                    "goal": task.request.goal,
                    "platform": task.request.platform,
                    "mode": task.request.mode,
                    "status": task.status,
                    "run_id": task.run_id,
                    "result": task.result,
                    "error": task.error,
                }
                for task in reversed(self._tasks.values())
            ]


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


def create_app(
    service: ToolAgentService | None = None,
    chat_router: ChatIntentRouter | None = None,
) -> FastAPI:
    console = RunConsole(service, chat_router)
    app = FastAPI(title="GUIWeave Run Console", docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        client = request.client.host if request.client else ""
        if client not in {"127.0.0.1", "::1", "testclient"}:
            return HTMLResponse("Local access only", status_code=403)
        origin = request.headers.get("origin")
        if request.method != "GET" and origin and not LOCAL_ORIGIN.fullmatch(origin):
            return HTMLResponse("Local origin only", status_code=403)
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse((ASSET_ROOT / "index.html").read_text(encoding="utf-8"))

    @app.get("/assets/{name}")
    def asset(name: str) -> FileResponse:
        if name not in {"console.css", "environment.css", "console.js"}:
            raise HTTPException(status_code=404)
        return FileResponse(ASSET_ROOT / name)

    @app.get("/api/runs")
    def list_runs(
        platform: PlatformName | None = None,
        limit: int = 100,
    ):
        try:
            return {"runs": console.service.list_runs(platform=platform, limit=limit)}
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc

    @app.get("/api/runs/{run_id:path}/events")
    def get_events(run_id: str, limit: int = 200):
        try:
            events = console.service.get_run_events(run_id, limit=limit)
            projected = []
            for event in events:
                item = dict(event)
                screenshot_path = item.pop("screenshot_path", None)
                if screenshot_path:
                    frame_name = Path(str(screenshot_path)).name
                    try:
                        console.service.get_run_frame_path(run_id, frame_name)
                    except (FileNotFoundError, ValueError):
                        pass
                    else:
                        item["screenshot"] = {"name": frame_name}
                projected.append(item)
            return {"events": projected}
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc

    @app.get("/api/run-frame")
    def get_run_frame(run_id: str, frame: str):
        try:
            path = console.service.get_run_frame_path(run_id, frame)
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return FileResponse(path, media_type=media_type, filename=None)

    @app.get("/api/runs/{run_id:path}/artifacts/{artifact}")
    def get_artifact(run_id: str, artifact: str):
        try:
            path = (
                console.service.get_run_frame_path(run_id, artifact)
                if Path(artifact).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
                else console.service.get_artifact_path(run_id, artifact)
            )
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return FileResponse(path, media_type=media_type, filename=None)

    @app.get("/api/runs/{run_id:path}")
    def get_run(run_id: str):
        try:
            return console.service.get_run(run_id)
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc

    @app.get("/api/tasks")
    def list_tasks():
        return {"tasks": console.tasks()}

    @app.get("/api/chat")
    def get_chat():
        return console.chat()

    @app.post("/api/chat/messages")
    def post_chat_message(payload: ChatRequest):
        return {"turn": asdict(console.post_chat(payload))}

    @app.post("/api/chat/session", status_code=201)
    def new_chat_session():
        try:
            return console.new_chat_session()
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc

    @app.get("/api/environment/model")
    def model_environment():
        return console.service.check_model_environment().to_dict()

    @app.get("/api/environment/{platform}")
    def platform_environment(
        platform: PlatformName,
        cdp_url: str | None = None,
        adb_serial: str | None = None,
        headless: bool = True,
    ):
        android_serial = _normalize_android_serial(adb_serial)
        result = console.service.check_platform_environment(
            platform,
            **(
                {"cdp_url": cdp_url, "headless": headless}
                if platform == "browser"
                else {"serial": android_serial} if platform == "android"
                else {}
            ),
        )
        return {
            "ok": result.ok,
            "summary": result.summary,
            "details": list(result.lines),
        }

    @app.post("/api/tasks", status_code=202)
    def create_task(payload: RunRequest):
        try:
            task = console.submit(payload)
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc
        return {"task_id": task.task_id, "status": task.status}

    @app.post("/api/tasks/{task_id}/cancel", status_code=202)
    def cancel_task(task_id: str):
        try:
            console.cancel(task_id)
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc
        return {"task_id": task_id, "status": "cancelling"}

    return app


load_dotenv()
app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=7468, log_level="warning")


if __name__ == "__main__":
    main()
