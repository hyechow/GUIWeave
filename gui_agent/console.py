"""Local-only Run Console for supervising GUIWeave Tool Agent tasks."""

from __future__ import annotations

import mimetypes
import re
import secrets
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator

from gui_agent.core.tool_agent.service import ToolAgentService
from gui_agent.core.runtime.platforms import PlatformName


ASSET_ROOT = Path(__file__).resolve().parent / "console_assets"


def _normalize_android_serial(value: str | None) -> str | None:
    """Accept a bare device IP while preserving ordinary adb serials."""

    serial = (value or "").strip()
    if not serial:
        return None
    if re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", serial):
        return f"{serial}:5555"
    return serial


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1, max_length=4000)
    platform: PlatformName
    mode: Literal["run", "chat"] = "run"
    perception: Literal["vision-only", "enhanced"] = "enhanced"
    max_turns: int = Field(default=50, ge=1, le=50)
    cdp_url: str | None = Field(default=None, max_length=500)
    adb_serial: str | None = Field(default=None, max_length=200)
    headless: bool = True
    multi_action: bool = True
    show_hud: bool = False

    @field_validator("adb_serial", mode="before")
    @classmethod
    def normalize_adb_serial(cls, value: object) -> str | None:
        return _normalize_android_serial(None if value is None else str(value))


@dataclass
class ActiveTask:
    task_id: str
    request: RunRequest
    status: str = "queued"
    run_id: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)


class RunConsole:
    """Keep only ephemeral process handles; durable task state lives in run artifacts."""

    def __init__(self, service: ToolAgentService | None = None) -> None:
        self.service = service or ToolAgentService()
        self._tasks: dict[str, ActiveTask] = {}
        self._lock = threading.Lock()

    def submit(self, request: RunRequest) -> ActiveTask:
        # Reject stale page options at the server boundary: Console is background-only.
        request = request.model_copy(update={"headless": True, "show_hud": False})
        task = ActiveTask(task_id=f"task_{secrets.token_hex(6)}", request=request)
        with self._lock:
            active = next(
                (
                    item
                    for item in self._tasks.values()
                    if item.request.platform == request.platform
                    and item.status in {"queued", "running", "cancelling"}
                ),
                None,
            )
            if active is not None:
                raise ValueError(
                    f"{request.platform} already has an active task: {active.task_id}"
                )
            self._tasks[task.task_id] = task
        thread = threading.Thread(
            target=self._run_task,
            args=(task,),
            name=f"guiweave-console-{task.task_id}",
            daemon=True,
        )
        thread.start()
        return task

    def _run_task(self, task: ActiveTask) -> None:
        with self._lock:
            if task.cancel_event.is_set():
                task.status = "interrupted"
                return
            task.status = "running"
        request = task.request
        options: dict[str, object]
        if request.platform == "browser":
            options = {"cdp_url": request.cdp_url, "headless": request.headless}
        elif request.platform == "android":
            options = {"serial": request.adb_serial}
        else:
            options = {}

        def record_run(run_id: str, _run_dir: Path) -> None:
            with self._lock:
                task.run_id = run_id

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
            return
        with self._lock:
            task.run_id = result.run_id
            task.result = result_payload
            task.status = (
                "interrupted"
                if task.cancel_event.is_set()
                else "completed" if result.phase == "completed" else "failed"
            )

    def cancel(self, task_id: str) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise FileNotFoundError(f"unknown active task: {task_id}")
            if task.status not in {"queued", "running", "cancelling"}:
                raise ValueError(f"task is already {task.status}")
            task.cancel_event.set()
            task.status = "cancelling"

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


def create_app(service: ToolAgentService | None = None) -> FastAPI:
    console = RunConsole(service)
    app = FastAPI(title="GUIWeave Run Console", docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        client = request.client.host if request.client else ""
        if client not in {"127.0.0.1", "::1", "testclient"}:
            return HTMLResponse("Local access only", status_code=403)
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
