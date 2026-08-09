from __future__ import annotations

import json
from pathlib import Path

from gui_agent.reports import RunnerReportBuilder, save_report


def test_tool_agent_trace_populates_report_timeline(tmp_path: Path) -> None:
    (tmp_path / "context.json").write_text(json.dumps({
        "goal": "Return two labels",
        "raw_input": "Return two labels",
        "platform": "browser",
        "journal": {"schema_version": 4, "events": []},
        "outcome": {
            "phase": "completed",
            "verification": "confirmed",
            "summary": "computed",
            "output": '["alpha", "beta"]',
        },
        "models": {"tool_agent.master": "master", "tool_agent.worker": "worker"},
        "orchestrator": {"kind": "tool_agent", "perception_mode": "vision-only"},
    }), encoding="utf-8")
    (tmp_path / "screenshot_tool_agent_1.png").write_bytes(b"not-decoded-by-builder")
    transform = "def transform(rows):\n    return [row['label'] for row in rows[:2]]"
    (tmp_path / "tool_agent_trace.json").write_text(json.dumps({
        "phase": "completed",
        "output": ["alpha", "beta"],
        "trace": [
            {
                "index": 1,
                "event": "master_tool",
                "step": 1,
                "tool": "run_worker",
                "args": {"spec": {"actions": [{
                    "name": "compute_labels",
                    "capability": "python_transform",
                    "fixed_args": {"source": transform},
                }]}},
            },
            {
                "index": 2,
                "event": "observe",
                "frame_id": "frame:1",
                "mode": "vision-only",
                "chunks": [{"ref": "chunk:labels:1", "provider": "vision", "row_count": 2}],
                "collections": [{"ref": "collection:labels", "row_count": 2}],
            },
            {
                "index": 3,
                "event": "python_transform",
                "tool": "compute_labels",
                "data_ref": "collection:labels",
                "result_ref": {"ref": "result:1"},
            },
            {
                "index": 4,
                "event": "master_tool",
                "step": 2,
                "tool": "finish_task",
                "args": {"result_ref": "result:1"},
            },
        ],
    }), encoding="utf-8")

    data = RunnerReportBuilder().build(tmp_path)
    report_path = save_report(data, tmp_path / "report.html")
    html = report_path.read_text(encoding="utf-8")

    assert len(data.pages) == 1
    assert len(data.pages[0].steps) == 4
    assert data.stats == {"turns": 4, "executed": 4}
    assert "Master tool" in html
    assert "Observe + materialize refs" in html
    assert "chunk:labels:1" in html
    assert "def transform(rows)" in html
    assert "result:1" in html
