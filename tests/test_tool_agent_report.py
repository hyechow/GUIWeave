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
    for index in range(1, 6):
        (tmp_path / f"screenshot_tool_agent_{index}.png").write_bytes(
            b"not-decoded-by-builder"
        )
    transform = "def transform(inputs):\n    return [row['label'] for row in inputs[0][:2]]"
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
                    "name": "reveal_labels",
                    "capability": "scroll",
                }]}},
            },
            {
                "index": 2,
                "event": "observe",
                "frame_id": "frame:5",
                "mode": "vision-only",
                "chunks": [{"ref": "chunk:labels:1", "provider": "vision", "row_count": 2}],
                "collections": [{"ref": "collection:labels", "row_count": 2}],
            },
            {
                "index": 3,
                "event": "worker_state_recovered",
                "tool": "runtime_scroll_visible",
                "state": {"status": "collecting", "summary": "Collecting labels"},
            },
                {
                    "index": 4,
                    "event": "worker_decision",
                "frame_id": "frame:5",
                "step": 1,
                "tool": "runtime_scroll_visible",
                "memory_event_count": 3,
                "context_chars": 2048,
                "state": {
                    "status": "collecting",
                    "summary": "Reveal the remaining labels",
                    },
                    "token_usage": {
                        "input": 1000,
                        "output": 20,
                        "cached_input": 800,
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
                            "raw_output": "runtime_scroll_visible",
                            "parsed": {"status": "collecting"},
                        },
                    ],
                },
            {
                "index": 5,
                "event": "runtime_action",
                "tool": "runtime_scroll_visible",
                "action_type": "scroll",
                "status": "executed",
            },
            {
                "index": 6,
                "event": "transform_started",
                "transform_id": "compute_labels",
                "inputs": ["collection:labels"],
                "source": transform,
            },
            {
                "index": 7,
                "event": "transform_completed",
                "transform_id": "compute_labels",
                "result_ref": {"ref": "result:1"},
            },
            {
                "index": 8,
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

    assert len(data.pages) == 2
    assert len(data.pages[0].steps) == 1
    assert data.pages[1].title == "Runtime Transform · compute_labels"
    assert data.pages[1].steps[0].raw_screenshot_url == "screenshot_tool_agent_5.png"
    assert data.pages[1].steps[0].display_label == "来源 GUI T5"
    assert data.stats == {"workers": 1, "turns": 2, "executed": 2}
    assert "GUI Worker · gui_worker" in html
    assert "collecting · runtime_scroll_visible" in html
    assert "Worker state recovery" not in html
    assert "chunk:labels:1" in html
    assert "def transform(inputs)" in html
    assert "result:1" in html
    assert "frame metadata" in html
    assert "compute_labels" in html
    assert "来源 GUI T5 · 命令" in html
    assert "rebuilt_per_frame" in html
    assert "2048" in html
    assert "journal_events" in html
    assert data.pages[0].steps[0].token_usage["tool_agent.worker"] == {
        "input": 1000,
        "output": 20,
        "cached_input": 800,
    }
    observation = data.pages[0].steps[0].non_ui["outputs"]["observation"]
    assert "controls" not in observation
    assert "cache 800 (80%)" in html


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
        "reply": "The result is 1.",
        "orchestrator": {"kind": "tool_agent", "perception_mode": "enhanced"},
    }), encoding="utf-8")
    source = (
        "def run(ctx):\n"
        "    collected = ctx.worker_result('collect')\n"
        "    result = ctx.transform(transform_id='shape', inputs=[collected['collection_ref']['ref']], source='def transform(inputs):\\n    return 1', result_schema={'type': 'integer'})\n"
        "    ctx.finish(result['ref'], effect='data')"
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
                "outcome": {"phase": "completed", "collection_ref": {"ref": "collection:one"}},
            },
            {
                "index": 4,
                "event": "transform_started",
                "transform_id": "shape",
                "inputs": ["collection:one"],
                "source": "def transform(inputs):\n    return 1",
            },
            {
                "index": 5,
                "event": "transform_completed",
                "transform_id": "shape",
                "result_ref": {"ref": "result:1"},
            },
            {
                "index": 6,
                "event": "master_program_completed",
                "phase": "completed",
                "result_ref": "result:1",
            },
        ],
    }), encoding="utf-8")
    (tmp_path / "tool_agent_replay.json").write_text(json.dumps({
        "status": "passed",
        "summary": "Deterministic replay passed",
        "program_count": 1,
        "gui_worker_count": 1,
        "uses_browser": False,
        "uses_llm": False,
    }), encoding="utf-8")
    (tmp_path / "tool_agent_presentation.json").write_text(json.dumps({
        "status": "generated",
        "reply": "The result is 1.",
        "result_digest": "a" * 64,
        "model": "presenter",
        "elapsed_s": 0.4,
        "llm_calls": 1,
        "token_usage": {"input": 40, "output": 8},
        "context_reports": [],
        "error": "",
    }), encoding="utf-8")

    data = RunnerReportBuilder().build(tmp_path)
    html = save_report(data, tmp_path / "report.html").read_text(encoding="utf-8")

    assert data.pages[0].title == "GUI Worker · collect"
    assert "Goal: Collect one result" in data.pages[0].statement_description
    assert "Coding Master · Python orchestration" in html
    assert "GUI Worker · collect" in html
    assert "def run(ctx)" in html
    assert "worker_result" in html
    assert "Replay · 通过" in html
    assert "0 LLM / 0 browser" in html
    assert "Presentation · User-facing reply" in html
    assert "input: goal + public result + replay verdict" in html
    assert "The result is 1." in html
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


def test_tool_agent_report_groups_ordered_actions_and_safe_suffix_abort(
    tmp_path: Path,
) -> None:
    (tmp_path / "context.json").write_text(json.dumps({
        "goal": "Exercise ordered GUI actions",
        "raw_input": "Exercise ordered GUI actions",
        "platform": "browser",
        "journal": {"schema_version": 4, "events": []},
        "outcome": {"phase": "completed", "summary": "done"},
        "orchestrator": {"kind": "tool_agent", "perception_mode": "enhanced"},
    }), encoding="utf-8")
    for index in (1, 2):
        (tmp_path / f"screenshot_tool_agent_{index}.png").write_bytes(b"not-an-image")
    state = {
        "status": "exploring",
        "summary": "Continue through the visible controls",
    }
    batches = [
        ([
            {"name": "runtime_tap_visible", "args": {"x": 500, "y": 400, "description": "Focus Product input"}},
            {"name": "runtime_clear_focused", "args": {}},
            {"name": "runtime_type_visible", "args": {"x": 500, "y": 400, "text": "Erica", "description": "Type Product query"}},
        ], 3, ""),
        ([
            {"name": "runtime_scroll_visible", "args": {"direction": "down", "description": "Reveal next row"}},
            {"name": "runtime_tap_visible", "args": {"x": 800, "y": 800, "description": "Open revealed row"}},
        ], 1, "scroll invalidated the remaining coordinates"),
    ]
    trace = [
        {"event": "worker_started", "worker_id": "ordered-worker", "profile": "operator", "goal": "Exercise ordered GUI actions"},
    ]
    for step, (actions, executed, reason) in enumerate(batches, 1):
        trace.extend([
            {"event": "observe", "worker_id": "ordered-worker", "frame_id": f"frame:{step}"},
            {"event": "worker_decision", "worker_id": "ordered-worker", "step": step, "tool": "continue_with_actions", "args": {"actions": actions}, "state": state},
            *({"event": "runtime_action", "tool": item["name"], "status": "executed"} for item in actions[:executed]),
            {"event": "worker_multi_action_aborted" if reason else "worker_multi_action_completed", "planned_actions": len(actions), "executed_actions": executed, "reason": reason},
        ])
    trace.append({"event": "runtime_finished", "phase": "completed", "summary": "done"})
    (tmp_path / "tool_agent_trace.json").write_text(json.dumps({
        "phase": "completed",
        "trace": trace,
    }), encoding="utf-8")

    data = RunnerReportBuilder().build(tmp_path)
    html = save_report(data, tmp_path / "report.html").read_text(encoding="utf-8")

    assert len(data.pages) == 1
    assert len(data.pages[0].steps) == 2
    first_batch = data.pages[0].steps[0].action_batch
    second_batch = data.pages[0].steps[1].action_batch
    assert first_batch is not None and first_batch["executed"] == 3
    assert second_batch is not None and second_batch["status"] == "aborted"
    assert second_batch["actions"][1]["status"] == "discarded"
    assert "动作批次 · 顺序执行" in html
    assert "动作批次 · 安全截断" in html
    assert "runtime_clear_focused" in html
    assert "scroll invalidated the remaining coordinates" in html
