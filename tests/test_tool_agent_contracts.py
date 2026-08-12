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
    MAX_ORDERED_ACTIONS,
    RequestActionPatchArgs,
    cacheable_system_message,
    diagnostic_prompt_reports,
    dynamic_action_tool,
    dynamic_worker_tools,
    exactly_one_tool_call,
    materialize_action_patch,
    normalize_action_arguments,
    response_usage,
    worker_action_floor,
)


_TARGET_DESCRIPTION = "Target the visible named control"


def test_action_envelope_preserves_dynamic_atomic_schemas() -> None:
    tools = dynamic_worker_tools(
        worker_action_floor(),
        completion_mode="operator",
        action_envelope=True,
    )
    names = {tool["function"]["name"] for tool in tools}

    assert "continue_with_actions" in names
    assert "runtime_type_visible" not in names
    envelope = next(
        tool for tool in tools
        if tool["function"]["name"] == "continue_with_actions"
    )
    parameters = envelope["function"]["parameters"]
    state = {
        "status": "exploring",
        "summary": "The complete form is visible.",
        "next_instruction": "Fill and submit the form.",
    }
    actions = [
        {
            "name": "runtime_type_visible",
            "args": {
                "x": 500,
                "y": 400,
                "text": "demo-user",
                "description": "Enter the visible Username input",
            },
        },
        {
            "name": "runtime_tap_visible",
            "args": {
                "x": 500,
                "y": 600,
                "description": "Tap the visible submit button",
            },
        },
    ]
    validate(instance={"state": state, "actions": actions}, schema=parameters)
    assert parameters["properties"]["actions"]["maxItems"] == MAX_ORDERED_ACTIONS
    with pytest.raises(ValidationError):
        validate(
            instance={
                "state": state,
                "actions": [actions[0]] * (MAX_ORDERED_ACTIONS + 1),
            },
            schema=parameters,
        )


def test_explicit_cache_marker_wraps_only_the_stable_system_prefix() -> None:
    message = cacheable_system_message(
        "stable policy",
        enabled=True,
        suffix="dynamic attempt",
    )

    assert message.content == [
        {
            "type": "text",
            "text": "stable policy",
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "text", "text": "dynamic attempt"},
    ]
@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            SimpleNamespace(
                usage_metadata={
                    "input_tokens": 1_000,
                    "output_tokens": 20,
                    "total_tokens": 1_020,
                    "input_token_details": {"cache_read": 800},
                },
                response_metadata={},
            ),
            {"input": 1_000, "output": 20, "total": 1_020, "cached_input": 800},
        ),
        (
            SimpleNamespace(
                usage_metadata={},
                response_metadata={
                    "token_usage": {
                        "prompt_tokens": 500,
                        "completion_tokens": 10,
                        "total_tokens": 510,
                        "prompt_tokens_details": {"cached_tokens": 320},
                    },
                },
            ),
            {"input": 500, "output": 10, "total": 510, "cached_input": 320},
        ),
    ],
)
def test_response_usage_normalizes_provider_cache_metadata(
    response: SimpleNamespace,
    expected: dict[str, int],
) -> None:
    assert response_usage(response) == expected


def test_response_usage_clamps_invalid_cached_token_counts() -> None:
    response = SimpleNamespace(
        usage_metadata={
            "input_tokens": 100,
            "output_tokens": 5,
            "input_token_details": {"cache_read": 900},
        },
        response_metadata={},
    )

    assert response_usage(response)["cached_input"] == 100


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

    assert set(parameters["properties"]) == {"amount", "x", "y", "description"}
    validate(instance={"amount": "medium", "description": _TARGET_DESCRIPTION}, schema=parameters)
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
    assert set(action.exposed_args) == {"x", "y", "description"}
    parameters = dynamic_action_tool(action)["function"]["parameters"]
    assert set(parameters["properties"]) == {"x", "y", "description"}
    assert set(parameters["required"]) == {"x", "y", "description"}
    with pytest.raises(ValidationError):
        validate(instance={}, schema=parameters)
    validate(instance={"x": 125, "y": 750, "description": _TARGET_DESCRIPTION}, schema=parameters)


def test_scroll_coordinates_are_optional_worker_owned_anchors() -> None:
    action = DynamicActionSpec(
        name="reveal_more",
        capability="scroll",
        description="Reveal another visual window",
        fixed_args={"direction": "down", "x": 0, "y": 0},
        exposed_args=[],
    )

    assert action.fixed_args == {"direction": "down"}
    assert set(action.exposed_args) == {"x", "y", "description"}
    parameters = dynamic_action_tool(action)["function"]["parameters"]
    assert set(parameters["properties"]) == {"x", "y", "description"}
    assert parameters["required"] == ["description"]
    validate(instance={"description": _TARGET_DESCRIPTION}, schema=parameters)


def test_select_option_keeps_value_fixed_but_coordinates_worker_owned() -> None:
    action = DynamicActionSpec(
        name="choose_required_status",
        capability="select_option",
        description="Choose the required status from the visible control",
        fixed_args={"text": "Complete", "x": 0, "y": 0},
        exposed_args=[],
    )

    assert action.fixed_args == {"text": "Complete"}
    assert set(action.exposed_args) == {"x", "y", "description"}
    tool = dynamic_action_tool(action)["function"]
    parameters = tool["parameters"]
    assert set(parameters["properties"]) == {"x", "y", "description"}
    assert set(parameters["required"]) == {"x", "y", "description"}
    assert "cannot execute a different recovery value" in tool["description"]
    validate(instance={"x": 800, "y": 500, "description": _TARGET_DESCRIPTION}, schema=parameters)


def test_select_option_exposes_value_when_master_does_not_bind_it() -> None:
    action = DynamicActionSpec(
        name="choose_visible_option",
        capability="select_option",
        description="Choose the option that advances the goal",
    )

    assert action.fixed_args == {}
    assert set(action.exposed_args) == {"x", "y", "text", "description"}
    parameters = dynamic_action_tool(action)["function"]["parameters"]
    assert set(parameters["required"]) == {"x", "y", "text", "description"}
    validate(
        instance={"x": 800, "y": 500, "text": "Complete", "description": _TARGET_DESCRIPTION},
        schema=parameters,
    )


def test_runtime_action_floor_and_patch_tool_are_always_available() -> None:
    floor = worker_action_floor()
    tools = dynamic_worker_tools(floor)
    names = {tool["function"]["name"] for tool in tools}

    assert {
        "runtime_tap_visible",
        "runtime_type_visible",
        "runtime_scroll_visible",
        "runtime_clear_focused",
        "runtime_press_enter",
        "runtime_select_visible",
        "runtime_open_url",
        "runtime_browser_back",
    }.issubset(names)
    assert {"request_action_patch", "complete", "fail"}.issubset(names)
    for tool in tools:
        parameters = tool["function"]["parameters"]
        assert "state" in parameters["properties"]
        assert "state" in parameters["required"]
    runtime_tap = next(
        tool for tool in tools
        if tool["function"]["name"] == "runtime_tap_visible"
    )
    assert "description" in runtime_tap["function"]["parameters"]["required"]

    runtime_select = next(
        tool for tool in tools
        if tool["function"]["name"] == "runtime_select_visible"
    )
    assert set(runtime_select["function"]["parameters"]["required"]) == {
        "state", "x", "y", "text", "description"
    }
    runtime_open_url = next(
        tool for tool in tools
        if tool["function"]["name"] == "runtime_open_url"
    )
    assert set(runtime_open_url["function"]["parameters"]["required"]) == {
        "state", "url"
    }
    assert "Runtime rejects inferred routes" in (
        runtime_open_url["function"]["description"]
    )
    validate(
        instance={
            "state": {
                "status": "exploring",
                "summary": "The exact target route is known.",
                "next_instruction": "Open it directly.",
            },
            "url": "/admin/review/product/index/",
        },
        schema=runtime_open_url["function"]["parameters"],
    )


def test_android_runtime_action_floor_excludes_browser_only_capabilities() -> None:
    floor = worker_action_floor({
        "tap",
        "type",
        "clear_text",
        "press_enter",
        "scroll",
        "back",
    })
    actions = {action.name: action.capability for action in floor}

    assert actions == {
        "runtime_tap_visible": "tap",
        "runtime_scroll_visible": "scroll",
        "runtime_type_visible": "type",
        "runtime_clear_focused": "clear_text",
        "runtime_press_enter": "press_enter",
        "runtime_back": "back",
    }
    assert "runtime_open_url" not in actions
    assert "runtime_select_visible" not in actions


def test_collector_completion_tool_is_frame_gated_and_runtime_bound() -> None:
    floor = worker_action_floor()

    waiting = dynamic_worker_tools(floor, completion_mode="unavailable")
    assert "complete" not in {tool["function"]["name"] for tool in waiting}

    ready = dynamic_worker_tools(floor, completion_mode="collector")
    complete = next(
        tool for tool in ready if tool["function"]["name"] == "complete"
    )
    properties = complete["function"]["parameters"]["properties"]
    assert "collection_ref" not in properties
    assert set(properties) == {"state", "evidence"}


def test_provider_coordinate_variants_are_normalized_before_strict_validation() -> None:
    assert normalize_action_arguments({"x": [36, 181], "y": [36, 181]}) == {
        "x": 36,
        "y": 181,
    }
    assert normalize_action_arguments({"point": [125, 750], "text": "value"}) == {
        "x": 125,
        "y": 750,
        "text": "value",
    }
    assert normalize_action_arguments({"x": "[36, 181]", "y": "[36, 181]"}) == {
        "x": 36,
        "y": 181,
    }
    assert normalize_action_arguments({"x": "125", "y": "750.5"}) == {
        "x": 125,
        "y": 750.5,
    }


def test_tool_call_accepts_json_encoded_argument_object() -> None:
    call = exactly_one_tool_call(SimpleNamespace(tool_calls=[{
        "id": "call-1",
        "name": "runtime_tap_visible",
        "args": '{"x": 125, "y": 750}',
    }]))

    assert call["args"] == {"x": 125, "y": 750}


def test_runtime_type_action_keeps_text_dynamic_and_coordinates_visual() -> None:
    action = next(
        item for item in worker_action_floor()
        if item.name == "runtime_type_visible"
    )

    assert action.capability == "type"
    assert action.fixed_args == {}
    assert set(action.exposed_args) == {
        "x", "y", "text", "description"
    }
    parameters = dynamic_action_tool(action)["function"]["parameters"]
    description = dynamic_action_tool(action)["function"]["description"]
    assert "recovery needs a value different" in description
    validate(
        instance={
            "x": 200,
            "y": 380,
            "text": "01/01/2023",
            "description": "Enter the date into the visible start-date input",
        },
        schema=parameters,
    )


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
    assert set(action.exposed_args) == {"x", "y", "description"}


def test_open_url_is_worker_owned_or_can_be_bound_by_action_patch() -> None:
    dynamic = DynamicActionSpec(
        name="open_goal_url",
        capability="open_url",
        description="Open the URL required by the current subgoal",
    )
    assert dynamic.fixed_args == {}
    assert dynamic.exposed_args == ["url"]

    bound = materialize_action_patch(RequestActionPatchArgs(
        name="open_known_url",
        capability="open_url",
        description="Open the known URL required by the current subgoal",
        url="https://example.test/records",
        reason="The exact target URL is present in task knowledge.",
    ))
    assert bound.fixed_args == {"url": "https://example.test/records"}
    assert bound.exposed_args == []


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


def test_gui_action_contract_rejects_python_transform() -> None:
    with pytest.raises(PydanticValidationError):
        DynamicActionSpec(
            name="shape_private_rows",
            capability="python_transform",
            description="Shape collected rows",
        )


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


def test_data_requirement_records_canonical_datetime_contract() -> None:
    requirement = DataRequirement(
        id="records",
        description="dated records",
        row_schema={"recorded_at": "string"},
        field_types={"recorded_at": "datetime"},
    )

    assert requirement.row_schema["properties"]["recorded_at"] == {
        "type": "string",
        "format": "date-time",
    }


def test_data_requirement_rejects_incompatible_runtime_type() -> None:
    with pytest.raises(ValueError, match="is incompatible"):
        DataRequirement(
            id="records",
            description="dated records",
            row_schema={"recorded_at": "number"},
            field_types={"recorded_at": "datetime"},
        )


def test_data_requirement_accepts_nullable_runtime_type() -> None:
    requirement = DataRequirement(
        id="records",
        description="optional labels",
        row_schema={
            "type": "object",
            "properties": {"label": {"type": ["string", "null"]}},
        },
        field_types={"label": "text"},
    )

    assert requirement.row_schema["properties"]["label"]["type"] == [
        "string",
        "null",
    ]


def test_worker_profile_is_inferred_without_creating_distinct_worker_types() -> None:
    common = {
        "goal": "Reach the requested outcome",
        "success_criteria": ["The outcome is reached"],
        "actions": [DynamicActionSpec(
            name="advance",
            capability="tap",
            description="Advance the current goal",
        )],
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


def test_data_requirement_rejects_filter_missing_from_observable_row_schema() -> None:
    with pytest.raises(ValueError, match="filter fields must be present"):
        DataRequirement(
            id="records",
            description="Filtered records",
            row_schema={"record_id": "string"},
            filters={"status": "Complete"},
        )


def test_data_requirement_rejects_declared_fields_missing_from_row_schema() -> None:
    with pytest.raises(ValueError, match="filter fields must be present"):
        DataRequirement(
            id="reviews",
            description="Reviews for one product",
            row_schema={
                "type": "object",
                "properties": {"rating": {"type": "number"}},
                "required": ["rating"],
            },
            field_sources={"rating": "Detailed Rating", "product_name": "Product"},
            field_types={"rating": "number", "product_name": "text"},
            filters={"product_name": "Selene Yoga Hoodie"},
        )


def test_worker_repairs_string_success_criteria_to_single_item_list() -> None:
    spec = WorkerSpec.model_validate({
        "profile": "operator",
        "goal": "Save the target",
        "success_criteria": "The target is saved successfully.",
        "data_requirements": [],
        "actions": [{
            "name": "save_target",
            "capability": "tap",
            "description": "Save the target",
        }],
    })

    assert spec.success_criteria == ["The target is saved successfully."]


def test_worker_normalizes_explicit_null_input_refs_to_empty_mapping() -> None:
    spec = WorkerSpec.model_validate({
        "profile": "operator",
        "goal": "Save the target",
        "success_criteria": ["The target is saved"],
        "input_refs": None,
        "data_requirements": [],
        "actions": [{
            "name": "save_target",
            "capability": "tap",
            "description": "Save the target",
        }],
    })

    assert spec.input_refs == {}


def test_worker_normalizes_string_action_input_shorthand() -> None:
    spec = WorkerSpec.model_validate({
        "profile": "operator",
        "goal": "Search for the next record",
        "success_criteria": ["The record is visible"],
        "input_refs": {"known_query": "result:query"},
        "actions": [
            {
                "name": "search_known",
                "capability": "type",
                "description": "Search using a Runtime-owned value",
                "input_args": {"text": "known_query"},
            },
            {
                "name": "search_observed",
                "capability": "type",
                "description": "Search using a value derived from the current UI",
                "input_args": {"text": "derived_query"},
            },
        ],
    })

    assert spec.actions[0].input_args["text"].input == "known_query"
    assert "text" not in spec.actions[0].exposed_args
    assert spec.actions[1].input_args == {}
    assert "text" in spec.actions[1].exposed_args


def test_worker_clamps_model_requested_steps_to_protocol_limit() -> None:
    spec = WorkerSpec.model_validate({
        "profile": "operator",
        "goal": "Complete a multi-page workflow",
        "success_criteria": ["The workflow is saved"],
        "data_requirements": [],
        "actions": [{
            "name": "advance",
            "capability": "tap",
            "description": "Advance the workflow",
        }],
        "max_steps": 30,
    })

    assert spec.max_steps == 20


def test_operator_rejects_acquisition_filters_even_when_action_has_same_value() -> None:
    common = {
        "profile": "operator",
        "goal": "Open one product",
        "success_criteria": ["The product editor is open"],
        "data_requirements": [],
        "actions": [{
            "name": "search_product",
            "capability": "type",
            "description": "Search the product grid",
            "fixed_args": {"text": "Selene Yoga Hoodie"},
        }],
    }

    for acquisition_filters in (
        {"Name": "Selene Yoga Hoodie"},
        {"Type": "Configurable Product"},
    ):
        with pytest.raises(ValueError, match="operator profile cannot declare"):
            WorkerSpec.model_validate({
                **common,
                "acquisition_filters": acquisition_filters,
            })
