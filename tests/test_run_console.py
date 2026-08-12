from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from gui_agent.console import RunConsole, RunRequest, create_app
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


class _BlockingService:
    def __init__(self, log_root: Path) -> None:
        self.log_root = log_root
        self.started = threading.Event()

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

    assert console.cancel(task.task_id).status == "cancelling"
    deadline = time.monotonic() + 1
    while task.status == "cancelling" and time.monotonic() < deadline:
        time.sleep(0.005)
    assert task.status == "interrupted"
