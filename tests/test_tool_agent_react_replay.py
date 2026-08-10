from __future__ import annotations

import json
from pathlib import Path

from gui_agent.core.tool_agent.contracts import (
    DynamicActionSpec,
    MaterializedFrame,
    WorkerSpec,
)
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


def test_task193_turn3_replay_requires_react_recovery_before_completion() -> None:
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
        actions=[DynamicActionSpec(
            name="filter_status_complete",
            capability="select_option",
            description="Select Complete in the Status filter",
            fixed_args={"text": "Complete"},
        )],
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
    active_actions = runtime._initial_worker_actions(spec)

    tools = runtime._worker_tools_for_frame(spec, active_actions, frame)
    tool_names = {tool["function"]["name"] for tool in tools}
    projection = project_worker_context(
        memory=build_worker_memory_view(WorkerJournal(worker_id="collector")),
        frame=frame,
    )

    assert case["expected_recovery"]["forbidden_tool"] not in tool_names
    assert "runtime_tap_visible" in tool_names
    assert any(control.get("label") == "Clear all" for control in frame.controls)
    assert '"extra_applied_filters": ["Purchase Date"]' in projection.text
    assert frame.collections == []

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
