from __future__ import annotations

import json
from pathlib import Path

import pytest

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


def test_get_run_rejects_path_traversal(tmp_path: Path) -> None:
    service = ToolAgentService(log_root=tmp_path / "logs")

    with pytest.raises(ValueError, match="outside"):
        service.get_run("../../private-run")
