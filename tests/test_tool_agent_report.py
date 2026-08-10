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
                "event": "worker_state_recovered",
                "tool": "compute_labels",
                "state": {"status": "computing", "summary": "Ready to transform"},
            },
                {
                    "index": 4,
                    "event": "worker_decision",
                "frame_id": "frame:1",
                "step": 1,
                "tool": "compute_labels",
                "state": {
                    "status": "computing",
                    "summary": "Coverage is complete; transform the collection",
                        "next_instruction": "Transform",
                    },
                    "context_reports": [
                        {
                            "kind": "prompt_snapshot",
                            "label": "tool_agent.worker",
                            "roles": [{
                                "role": "human",
                                "parts": [{
                                    "label": "frame",
                                    "type": "text",
                                    "text": "frame metadata",
                                    "chars": 14,
                                }],
                            }],
                        },
                        {
                            "kind": "llm_output",
                            "label": "tool_agent.worker",
                            "raw_output": "compute_labels",
                            "parsed": {"status": "computing"},
                        },
                    ],
                },
            {
                "index": 5,
                "event": "python_transform",
                "tool": "compute_labels",
                "data_ref": "collection:labels",
                "result_ref": {"ref": "result:1"},
            },
            {
                "index": 6,
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
    assert len(data.pages[0].steps) == 1
    assert data.stats == {"workers": 1, "turns": 1, "executed": 1}
    assert "GUI Worker · gui_worker" in html
    assert "computing · compute_labels" in html
    assert "Worker state recovery" not in html
    assert "chunk:labels:1" in html
    assert "def transform(rows)" in html
    assert "result:1" in html
    assert "frame metadata" in html
    assert "compute_labels" in html


def test_tool_agent_report_exposes_coding_master_and_worker_boundaries(tmp_path: Path) -> None:
    (tmp_path / "context.json").write_text(json.dumps({
        "goal": "Produce one result",
        "raw_input": "Produce one result",
        "platform": "browser",
        "journal": {"schema_version": 4, "events": []},
        "outcome": {
            "phase": "completed",
            "verification": "confirmed",
            "summary": "done",
            "output": "1",
        },
        "orchestrator": {"kind": "tool_agent", "perception_mode": "enhanced"},
    }), encoding="utf-8")
    source = (
        "def run(ctx):\n"
        "    result = ctx.worker_result('collect')\n"
        "    ctx.finish(result['result_ref']['ref'])"
    )
    (tmp_path / "tool_agent_trace.json").write_text(json.dumps({
        "phase": "completed",
        "output": 1,
        "trace": [
            {
                "index": 1,
                "event": "master_program_generated",
                "generation": 1,
                "source": source,
            },
            {
                "index": 2,
                "event": "master_worker_dispatch",
                "worker_id": "collect",
                "kind": "gui",
                "goal": "Collect one result",
            },
            {
                "index": 3,
                "event": "master_worker_result",
                "worker_id": "collect",
                "outcome": {"phase": "completed", "result_ref": {"ref": "result:1"}},
            },
            {
                "index": 4,
                "event": "master_program_completed",
                "phase": "completed",
                "result_ref": "result:1",
            },
        ],
    }), encoding="utf-8")

    data = RunnerReportBuilder().build(tmp_path)
    html = save_report(data, tmp_path / "report.html").read_text(encoding="utf-8")

    assert data.pages[0].title == "GUI Worker · collect"
    assert "Coding Master · Python orchestration" in html
    assert "GUI Worker · collect" in html
    assert "def run(ctx)" in html
    assert "worker_result" in html
    assert data.pages[0].verify_outcome["status"] == "done"


def test_tool_agent_report_can_recover_from_streamed_events_without_final_trace(
    tmp_path: Path,
) -> None:
    (tmp_path / "context.json").write_text(json.dumps({
        "goal": "Continue until interrupted",
        "raw_input": "Continue until interrupted",
        "platform": "browser",
        "journal": {"schema_version": 4, "events": []},
        "outcome": {
            "phase": "failed",
            "verification": "unknown",
            "summary": "interrupted",
        },
        "orchestrator": {"kind": "tool_agent", "perception_mode": "enhanced"},
    }), encoding="utf-8")
    events = [
        {
            "index": 1,
            "elapsed_s": 0.1,
            "layer": "worker",
            "event": "worker_started",
            "message": "Start collector loop",
            "profile": "collector",
            "goal": "Collect records",
        },
        {
            "index": 2,
            "elapsed_s": 0.2,
            "layer": "runtime",
            "event": "runtime_interrupted",
            "message": "Tool Agent interrupted",
            "summary": "Tool Agent interrupted",
        },
    ]
    (tmp_path / "tool_agent_events.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )

    data = RunnerReportBuilder().build(tmp_path)
    html = save_report(data, tmp_path / "report.html").read_text(encoding="utf-8")

    assert len(data.pages) == 1
    assert len(data.pages[0].steps) == 0
    assert data.stats == {"workers": 1, "turns": 0, "executed": 0}
    assert "Tool Agent interrupted" in html
    assert data.pages[0].verify_outcome["status"] == "failed"
