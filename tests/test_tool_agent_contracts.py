from __future__ import annotations

from types import SimpleNamespace

import pytest
from jsonschema import ValidationError, validate
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import ValidationError as PydanticValidationError

from gui_agent.core.tool_agent.contracts import (
    DataRequirement,
    DynamicActionSpec,
    WorkerSpec,
    WorkerState,
)
from gui_agent.core.tool_agent.protocol import (
    RequestActionPatchArgs,
    diagnostic_prompt_reports,
    dynamic_action_tool,
    dynamic_worker_tools,
    materialize_action_patch,
    worker_action_floor,
)


def test_diagnostic_prompt_reports_omit_image_payloads() -> None:
    reports = diagnostic_prompt_reports(
        "tool_agent.worker",
        [
            SystemMessage(content="policy"),
            HumanMessage(content=[
                {"type": "text", "text": "frame metadata"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,SECRET"}},
            ]),
        ],
        SimpleNamespace(
            content='{"status":"exploring"}',
            tool_calls=[{"name": "tap", "args": {"x": 1, "y": 2}}],
        ),
        parsed={"status": "exploring"},
    )

    rendered = str(reports)
    assert "SECRET" not in rendered
    assert "image_url omitted" in rendered
    assert reports[0]["kind"] == "prompt_snapshot"
    assert reports[1]["kind"] == "llm_output"


def test_dynamic_action_exposes_only_worker_decisions() -> None:
    action = DynamicActionSpec(
        name="reveal_more_records",
        capability="scroll",
        description="Reveal the next visual window",
        fixed_args={"direction": "down", "target_area": "main_content"},
        exposed_args=["amount"],
    )

    parameters = dynamic_action_tool(action)["function"]["parameters"]

    assert set(parameters["properties"]) == {"amount", "x", "y"}
    validate(instance={"amount": "medium"}, schema=parameters)
    with pytest.raises(ValidationError):
        validate(instance={"direction": "up"}, schema=parameters)


def test_tap_coordinates_are_always_owned_by_visual_worker() -> None:
    action = DynamicActionSpec(
        name="activate_visible_control",
        capability="tap",
        description="Activate the visible control that advances the goal",
        fixed_args={"x": 0, "y": 0},
        exposed_args=[],
    )

    assert action.fixed_args == {}
    assert set(action.exposed_args) == {"x", "y"}
    parameters = dynamic_action_tool(action)["function"]["parameters"]
    assert set(parameters["properties"]) == {"x", "y"}
    assert set(parameters["required"]) == {"x", "y"}
    with pytest.raises(ValidationError):
        validate(instance={}, schema=parameters)
    validate(instance={"x": 125, "y": 750}, schema=parameters)


def test_scroll_coordinates_are_optional_worker_owned_anchors() -> None:
    action = DynamicActionSpec(
        name="reveal_more",
        capability="scroll",
        description="Reveal another visual window",
        fixed_args={"direction": "down", "x": 0, "y": 0},
        exposed_args=[],
    )

    assert action.fixed_args == {"direction": "down"}
    assert set(action.exposed_args) == {"x", "y"}
    parameters = dynamic_action_tool(action)["function"]["parameters"]
    assert set(parameters["properties"]) == {"x", "y"}
    assert parameters["required"] == []
    validate(instance={}, schema=parameters)


def test_select_option_keeps_value_fixed_but_coordinates_worker_owned() -> None:
    action = DynamicActionSpec(
        name="choose_required_status",
        capability="select_option",
        description="Choose the required status from the visible control",
        fixed_args={"text": "Complete", "x": 0, "y": 0},
        exposed_args=[],
    )

    assert action.fixed_args == {"text": "Complete"}
    assert set(action.exposed_args) == {"x", "y"}
    parameters = dynamic_action_tool(action)["function"]["parameters"]
    assert set(parameters["properties"]) == {"x", "y"}
    assert set(parameters["required"]) == {"x", "y"}
    validate(instance={"x": 800, "y": 500}, schema=parameters)


def test_select_option_exposes_value_when_master_does_not_bind_it() -> None:
    action = DynamicActionSpec(
        name="choose_visible_option",
        capability="select_option",
        description="Choose the option that advances the goal",
    )

    assert action.fixed_args == {}
    assert set(action.exposed_args) == {"x", "y", "text"}
    parameters = dynamic_action_tool(action)["function"]["parameters"]
    assert set(parameters["required"]) == {"x", "y", "text"}
    validate(instance={"x": 800, "y": 500, "text": "Complete"}, schema=parameters)


def test_runtime_action_floor_and_patch_tool_are_always_available() -> None:
    floor = worker_action_floor()
    tools = dynamic_worker_tools(floor)
    names = {tool["function"]["name"] for tool in tools}

    assert {"runtime_tap_visible", "runtime_scroll_visible"}.issubset(names)
    assert {"request_action_patch", "complete", "fail"}.issubset(names)


def test_worker_can_materialize_registered_frame_driven_action() -> None:
    patch = RequestActionPatchArgs(
        name="choose_visible_status",
        capability="select_option",
        description="Choose the status required by the subgoal",
        option_text="Complete",
        reason="The current screenshot shows a named choice control.",
    )

    action = materialize_action_patch(patch)

    assert action.fixed_args == {"text": "Complete"}
    assert set(action.exposed_args) == {"x", "y"}


def test_worker_action_patch_registry_owns_scroll_parameters() -> None:
    patch = RequestActionPatchArgs(
        name="reveal_missing_region",
        capability="scroll",
        description="Reveal another part of the current subgoal surface",
        reason="Required content is outside the current viewport.",
    )

    action = materialize_action_patch(patch)

    assert action.fixed_args == {}
    assert set(action.exposed_args) == {
        "direction",
        "amount",
        "target_area",
        "description",
        "x",
        "y",
    }


def test_worker_action_patch_cannot_expand_into_python_execution() -> None:
    with pytest.raises(PydanticValidationError):
        RequestActionPatchArgs.model_validate(
            {
                "name": "invent_transform",
                "capability": "python_transform",
                "description": "not allowed",
                "reason": "not allowed",
            }
        )


def test_python_transform_ref_is_always_owned_by_worker_runtime() -> None:
    action = DynamicActionSpec(
        name="shape_private_rows",
        capability="python_transform",
        description="Shape collected rows",
        fixed_args={
            "data_ref": "master-cannot-bind-this",
            "source": "def transform(rows):\n    return rows",
        },
    )

    assert action.fixed_args == {"source": "def transform(rows):\n    return rows"}
    assert action.exposed_args == ["data_ref"]
    parameters = dynamic_action_tool(action)["function"]["parameters"]
    assert parameters["required"] == ["data_ref"]


def test_worker_state_can_report_missing_action_without_abandoning_subgoal() -> None:
    state = WorkerState.model_validate(
        {
            "status": "exploring",
            "summary": "A named choice control is visible.",
            "open_gaps": ["Need a select capability"],
            "action_space_status": "missing_action",
            "missing_action": "Select a named option from the visible control",
            "next_instruction": "Request the missing registered action on this frame.",
        }
    )

    assert state.action_space_status == "missing_action"
    assert state.missing_action


def test_dynamic_action_rejects_fixed_and_exposed_overlap() -> None:
    with pytest.raises(ValueError, match="fixed and exposed"):
        DynamicActionSpec(
            name="move",
            capability="scroll",
            description="move",
            fixed_args={"direction": "down"},
            exposed_args=["direction"],
        )


def test_data_requirement_expands_model_schema_shorthand() -> None:
    requirement = DataRequirement(
        id="records",
        description="visible records",
        row_schema={"label": "string", "count": "integer"},
    )

    assert requirement.row_schema == {
        "type": "object",
        "properties": {
            "label": {"type": "string"},
            "count": {"type": "integer"},
        },
        "required": ["label", "count"],
        "additionalProperties": False,
    }


def test_worker_profile_is_inferred_without_creating_distinct_worker_types() -> None:
    common = {
        "goal": "Reach the requested outcome",
        "success_criteria": ["The outcome is reached"],
        "actions": [DynamicActionSpec(
            name="advance",
            capability="tap",
            description="Advance the current goal",
        )],
        "result_schema": {"type": "boolean"},
    }

    operator = WorkerSpec.model_validate(common)
    collector = WorkerSpec.model_validate({
        **common,
        "data_requirements": [{
            "id": "records",
            "description": "Collect the requested records",
            "row_schema": {"value": "string"},
        }],
    })

    assert operator.profile == "operator"
    assert collector.profile == "collector"


def test_data_requirement_filter_must_be_observable_in_row_schema() -> None:
    with pytest.raises(ValueError, match="filter fields must be present"):
        DataRequirement(
            id="records",
            description="Filtered records",
            row_schema={"record_id": "string"},
            filters={"status": "Complete"},
        )
