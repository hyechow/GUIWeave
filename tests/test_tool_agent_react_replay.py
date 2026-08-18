from __future__ import annotations

import json
from pathlib import Path

import pytest

from gui_agent.core.tool_agent.contracts import (
    MaterializedFrame,
    WorkerSpec,
)
from gui_agent.core.tool_agent.data_store import RuntimeDataStore
from gui_agent.core.tool_agent.runtime import ToolAgentRuntime
from gui_agent.core.tool_agent.worker_memory import (
    WorkerJournal,
    build_worker_memory_view,
    project_worker_context,
)


_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "tool_agent"
    / "task193_stale_filter_recovery.json"
)


def test_task193_turn3_does_not_gain_an_undeclared_recovery_action() -> None:
    case = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    spec = WorkerSpec(
        profile="collector",
        goal=case["goal"],
        success_criteria=["All completed orders are collected"],
        data_requirements=[{
            "id": "completed_orders",
            "description": "Completed orders",
            "row_schema": {
                "type": "object",
                "properties": {"status": {"type": "string"}},
                "required": ["status"],
            },
            "field_sources": {"status": "Status"},
            "field_types": {"status": "text"},
            "filters": {"status": "Complete"},
        }],
        strategy={"approach": "Filter the orders to the requested status."},
    )
    frame = MaterializedFrame(
        frame_id=case["source_frame"],
        screenshot_path="recorded-task193-turn3.png",
        controls=case["controls"],
        applied_filters=case["applied_filters"],
        requirement_scopes={
            "completed_orders": {
                "status": "unmet",
                "requested_filters": case["required_filters"],
                "applied_filters": case["applied_filters"],
                "evidence": "applied_filter_state",
            },
        },
        missing_requirements=["completed_orders"],
    )
    runtime = object.__new__(ToolAgentRuntime)
    runtime._platform_capabilities = frozenset({"select_option"})
    active_actions = runtime._initial_worker_actions(spec)

    tools = runtime._worker_tools_for_frame(spec, active_actions, frame)
    tool_names = {tool["function"]["name"] for tool in tools}
    projection = project_worker_context(
        memory=build_worker_memory_view(WorkerJournal(worker_id="collector")),
        frame=frame,
    )

    # ReAct collection: complete is always offered to a collector on a ready frame.
    # The old mechanical gate withheld it here (the recorded run shows
    # complete_available=False below); the new guard is that calling it with zero
    # accumulated rows is rejected by the runtime with guidance.
    assert tool_names == {"select_option", "report_blocked", "complete"}
    assert any(control.get("label") == "Clear all" for control in frame.controls)
    assert '"extra_applied_filters": ["Purchase Date"]' in projection.text
    assert frame.collections == []

    runtime.data_store = RuntimeDataStore()
    with pytest.raises(ValueError, match="no accumulated rows"):
        runtime._execute_worker_tool(
            spec, active_actions, {"name": "complete", "args": {"evidence": []}},
            b"png", frame,
        )

    observed = case["observed_policy_replay"]
    assert observed["complete_available"] is False
    assert observed["tool_call"]["name"] == "runtime_tap_visible"
    args = observed["tool_call"]["args"]
    assert "Purchase Date" in args["description"]
    purchase_date_remove = next(
        control
        for control in frame.controls
        if control.get("label") == "Remove" and control["rect"]["x"] == 365
    )
    assert (args["x"], args["y"]) == (
        purchase_date_remove["rect"]["x"],
        purchase_date_remove["rect"]["y"],
    )
