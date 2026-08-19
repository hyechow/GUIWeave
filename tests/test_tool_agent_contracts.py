from __future__ import annotations

import base64
import json
from io import BytesIO
from types import SimpleNamespace

import pytest
from jsonschema import ValidationError, validate
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import ValidationError as PydanticValidationError
from PIL import Image

from gui_agent.core.tool_agent.contracts import (
    DataRequirement,
    DynamicActionSpec,
    WorkerState,
    WorkerSpec,
    approach_is_procedural,
)
from gui_agent.core.tool_agent.protocol import (
    MAX_ORDERED_ACTIONS,
    ProtocolError,
    cacheable_system_message,
    diagnostic_prompt_reports,
    decode_worker_action,
    dynamic_action_tool,
    dynamic_worker_tools,
    exactly_one_tool_call,
    image_message,
    json_worker_decision_instruction,
    normalize_action_arguments,
    response_usage,
    worker_attempt_contract,
    validate_worker_tool_state,
)


_TARGET_DESCRIPTION = "Target the visible named control"


@pytest.mark.parametrize(
    ("summary", "evidence", "rejected"),
    [
        ("The send button is visible and ready to activate.", [], True),
        ("The next step is to submit the completed form.", [], True),
        ("Reply composed.", ["Confirmation control is awaiting activation"], True),
        ("表单已填写，下一步需要点击提交按钮。", [], True),
        ("Submit executed and the editor closed without an error.", [], False),
        ("No further control is available; 不需要点击其他按钮。", [], False),
    ],
)
def test_worker_completion_rejects_pending_action_claims(
    summary: str,
    evidence: list[str],
    rejected: bool,
) -> None:
    state = WorkerState(status="completed", summary=summary)

    if rejected:
        with pytest.raises(ProtocolError, match="pending action or commit control"):
            validate_worker_tool_state("complete", state, {"evidence": evidence})
    else:
        validate_worker_tool_state("complete", state, {"evidence": evidence})


@pytest.mark.parametrize(
    "approach",
    [
        "web search engine weather query",
        "Bing search results page weather card",
        "tianqi.com weather forecast page",
    ],
)
def test_approach_noun_phrases_are_not_procedures(approach: str) -> None:
    assert not approach_is_procedural(approach)


@pytest.mark.parametrize(
    "approach",
    [
        "Type the query and press Enter",
        "Open the page, search for the record",
        "Use the current source then switch applications",
        "open_url https://weather.example/forecast/location-id",
        "https://weather.example/ forecast source",
        "Navigate to the public weather source",
        "direct navigation to China Weather Net (weather.com.cn) Shenzhen city page",
        "direct navigation to a dedicated weather service website",
    ],
)
def test_approach_execution_details_are_procedural(approach: str) -> None:
    assert approach_is_procedural(approach)


def _declared_form_actions() -> list[DynamicActionSpec]:
    return [
        DynamicActionSpec(
            name="enter_visible_value",
            capability="type",
            description="Enter a value in the visible input",
            exposed_args=["text"],
        ),
        DynamicActionSpec(
            name="submit_visible_form",
            capability="tap",
            description="Submit the visible form",
        ),
    ]


def test_image_message_resizes_without_changing_coordinate_contract() -> None:
    source = BytesIO()
    Image.new("RGB", (1280, 800), "white").save(source, format="PNG")

    message = image_message("frame", source.getvalue(), scale=0.75)

    encoded = message.content[1]["image_url"]["url"].split(",", 1)[1]
    with Image.open(BytesIO(base64.b64decode(encoded))) as image:
        assert image.size == (960, 600)


def test_image_message_rejects_invalid_scale() -> None:
    with pytest.raises(ValueError, match=r"image scale must be in \(0, 1\]"):
        image_message("frame", b"png", scale=0)


def test_action_envelope_preserves_dynamic_atomic_schemas() -> None:
    tools = dynamic_worker_tools(
        _declared_form_actions(),
        completion_mode="operator",
        action_envelope=True,
    )
    names = {tool["function"]["name"] for tool in tools}

    assert "continue_with_actions" in names
    assert "enter_visible_value" not in names
    envelope = next(
        tool for tool in tools
        if tool["function"]["name"] == "continue_with_actions"
    )
    description = envelope["function"]["description"]
    assert "excluded or already-processed candidates permit traversal" in description
    assert "call complete directly" in description
    assert "do not put terminal tools" in description
    assert "tap" not in description
    assert "clear_text" not in description
    assert "select_option" not in description
    parameters = envelope["function"]["parameters"]
    variants = parameters["properties"]["actions"]["items"]["oneOf"]
    assert [variant["description"] for variant in variants] == [
        "Enter a value in the visible input",
        "Submit the visible form",
    ]
    state_schema = parameters["properties"]["state"]
    assert set(state_schema["properties"]) == {
        "status", "summary", "established_facts",
    }
    state = {
        "status": "exploring",
        "summary": "The complete form is visible.",
        "established_facts": [],
    }
    actions = [
        {
            "name": "enter_visible_value",
            "args": {
                "x": 500,
                "y": 400,
                "text": "demo-user",
                "description": "Enter the visible Username input",
            },
        },
        {
            "name": "submit_visible_form",
            "args": {
                "x": 500,
                "y": 600,
                "description": "Tap the visible submit button",
            },
        },
    ]
    validate(instance={"state": state, "actions": actions}, schema=parameters)
    with pytest.raises(ValidationError):
        validate(
            instance={
                "state": {**state, "next_instruction": "Use a different source"},
                "actions": actions,
            },
            schema=parameters,
        )
    assert parameters["properties"]["actions"]["maxItems"] == MAX_ORDERED_ACTIONS
    with pytest.raises(ValidationError):
        validate(
            instance={
                "state": state,
                "actions": [actions[0]] * (MAX_ORDERED_ACTIONS + 1),
            },
            schema=parameters,
        )
    limited = dynamic_worker_tools(
        _declared_form_actions(),
        completion_mode="operator",
        action_envelope=True,
        max_ordered_actions=1,
    )
    envelope = next(
        tool for tool in limited if tool["function"]["name"] == "continue_with_actions"
    )
    assert envelope["function"]["parameters"]["properties"]["actions"]["maxItems"] == 1
    assert "exactly one action" in envelope["function"]["description"]
    assert [tool["function"]["name"] for tool in dynamic_worker_tools(
        [], completion_mode="unavailable", action_envelope=True,
    )] == [
        "report_blocked",
    ]


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


def test_collector_completion_tool_is_frame_gated_and_runtime_bound() -> None:
    actions = _declared_form_actions()

    waiting = dynamic_worker_tools(actions, completion_mode="unavailable")
    assert "complete" not in {tool["function"]["name"] for tool in waiting}
    action_state = waiting[0]["function"]["parameters"]["properties"]["state"]
    assert action_state["properties"]["status"]["enum"] == ["exploring", "collecting"]

    ready = dynamic_worker_tools(actions, completion_mode="collector")
    complete = next(
        tool for tool in ready if tool["function"]["name"] == "complete"
    )
    properties = complete["function"]["parameters"]["properties"]
    assert "collection_ref" not in properties
    assert set(properties) == {"state", "evidence", "rows"}
    assert properties["state"]["properties"]["status"]["enum"] == ["completed"]
    failure = next(
        tool for tool in ready if tool["function"]["name"] == "report_blocked"
    )
    failure_state = failure["function"]["parameters"]["properties"]["state"]
    assert failure_state["properties"]["status"]["enum"] == ["failed"]


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


def test_json_worker_protocol_preserves_dynamic_action_contract() -> None:
    tools = dynamic_worker_tools(
        [DynamicActionSpec(
            name="activate_visible_target",
            capability="tap",
            description="Activate the visible target",
        )],
        completion_mode="operator",
        action_envelope=True,
        max_ordered_actions=1,
    )
    instruction = json_worker_decision_instruction(tools)
    response = SimpleNamespace(content=(
        '{"tool":"complete","args":{"state":{"status":"completed",'
        '"summary":"Done","established_facts":[]},'
        '"evidence":["Visible target state confirmed"]}}'
    ))

    call, state, _calls = decode_worker_action(response, protocol="json")

    assert call["name"] == "complete"
    assert state["status"] == "completed"
    assert "continue_with_actions" in instruction
    assert '"maxItems":1' in instruction
    contract = json.loads(instruction.split("Available contract:\n", 1)[1])
    assert all(
        "state" in action["parameters"]["properties"]
        and "strategy_status" not in action["parameters"]["properties"]["state"]["properties"]
        for action in contract.values()
    )
    assert "never inside `args.actions[*]`" in instruction


def test_worker_protocol_normalizes_flat_ordered_action_arguments() -> None:
    raw_action = {
        "name": "tap", "x": 500, "y": 76,
        "description": "Tap the visible city selector at the top",
    }
    tools = dynamic_worker_tools(
        [DynamicActionSpec(
            name="tap", capability="tap", description="Tap one visible control.",
        )],
        completion_mode="unavailable",
        action_envelope=True,
    )
    response = SimpleNamespace(tool_calls=[{
        "name": "continue_with_actions",
        "args": {
            "state": {
                "status": "exploring",
                "summary": "The city selector is visible.",
                "established_facts": [],
            },
            "actions": [raw_action],
        },
    }])
    call, state, calls = decode_worker_action(
        response,
        tools={tool["function"]["name"]: tool for tool in tools},
    )

    expected = {"name": "tap", "args": {k: v for k, v in raw_action.items() if k != "name"}}
    assert state["status"] == "exploring"
    assert calls == call["args"]["actions"] == [expected]


def test_declared_type_action_keeps_text_dynamic_and_coordinates_visual() -> None:
    action = DynamicActionSpec(
        name="enter_visible_value",
        capability="type",
        description="Enter a value in the visible input",
        exposed_args=["text"],
    )

    assert action.capability == "type"
    assert action.fixed_args == {}
    assert set(action.exposed_args) == {
        "x", "y", "text", "description"
    }
    parameters = dynamic_action_tool(action)["function"]["parameters"]
    description = dynamic_action_tool(action)["function"]["description"]
    assert description == "Enter a value in the visible input"
    validate(
        instance={
            "x": 200,
            "y": 380,
            "text": "01/01/2023",
            "description": "Enter the date into the visible start-date input",
        },
        schema=parameters,
    )


def test_gui_action_contract_rejects_python_transform() -> None:
    with pytest.raises(PydanticValidationError):
        DynamicActionSpec(
            name="shape_private_rows",
            capability="python_transform",
            description="Shape collected rows",
        )


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


def test_worker_profile_inference_selects_relevant_attempt_rules() -> None:
    common = {
        "goal": "Reach the requested outcome",
        "success_criteria": ["The outcome is reached"],
        "strategy": {"approach": "Visible task surface"},
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
    collector_contract = worker_attempt_contract(collector)
    operator_contract = worker_attempt_contract(operator)
    for rule in (
        "natural relative-date term", "never scroll or paginate",
        "empty_authoritative = false",
    ):
        assert rule in collector_contract and rule not in operator_contract
    for rule in ("candidate_set_state.status = exhausted", "Finish comparison evidence"):
        assert rule in operator_contract and rule not in collector_contract


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


def test_worker_rejects_string_success_criteria() -> None:
    with pytest.raises(PydanticValidationError):
        WorkerSpec.model_validate({
            "profile": "operator",
            "goal": "Save the target",
            "success_criteria": "The target is saved successfully.",
            "strategy": {
                "approach": "Save through the visible editor.",
            },
        })


def test_worker_rejects_null_input_refs() -> None:
    with pytest.raises(PydanticValidationError):
        WorkerSpec.model_validate({
            "profile": "operator",
            "goal": "Save the target",
            "success_criteria": ["The target is saved"],
            "input_refs": None,
            "strategy": {
                "approach": "Save through the visible editor.",
            },
        })


def test_worker_rejects_string_action_input_shorthand() -> None:
    with pytest.raises(PydanticValidationError):
        WorkerSpec.model_validate({
            "profile": "operator",
            "goal": "Search for the next record",
            "success_criteria": ["The record is visible"],
            "input_refs": {"known_query": "result:query"},
            "input_bindings": [{
                "name": "search_known",
                "input": "known_query",
                "path": "name",
                "target": "text_input",
                "description": "Search using a Runtime-owned value",
            }],
            "strategy": {
                "approach": "Search for the Runtime-owned target.",
            },
        })


def test_worker_attempt_contract_keeps_input_binding_in_immutable_contract() -> None:
    spec = WorkerSpec.model_validate({
        "profile": "operator",
        "goal": "Find the Runtime-bound target",
        "success_criteria": ["The target is visible"],
        "input_refs": {"target": "result:1"},
        "input_bindings": [{
            "name": "enter_target",
            "input": "target",
            "path": ["name"],
            "target": "text_input",
            "description": "Enter the Runtime-bound target",
        }],
        "strategy": {
            "approach": "Search for the Runtime-bound target.",
        },
    })

    contract = worker_attempt_contract(spec)

    assert '"input_bindings"' in contract
    assert '"name": "enter_target"' in contract
    assert '"approach": "Search for the Runtime-bound target."' in contract


def test_worker_rejects_missing_input_binding_description() -> None:
    with pytest.raises(PydanticValidationError):
        WorkerSpec.model_validate({
            "profile": "operator",
            "goal": "Search using another visible source",
            "success_criteria": ["A relevant result is visible"],
            "input_refs": {"query": "result:query"},
            "input_bindings": [{
                "name": "enter_alternative_query",
                "input": "query",
                "target": "text_input",
            }],
            "strategy": {
                "approach": "Use another visible source.",
            },
        })


def test_worker_strategy_rejects_runtime_budget_fields() -> None:
    with pytest.raises(PydanticValidationError):
        WorkerSpec.model_validate({
            "profile": "operator",
            "goal": "Complete a multi-page workflow",
            "success_criteria": ["The workflow is saved"],
            "strategy": {
                "approach": "Advance through the workflow.",
                "max_steps": 30,
            },
        })


def test_worker_strategy_rejects_acquisition_filters() -> None:
    common = {
        "profile": "operator",
        "goal": "Open one product",
        "success_criteria": ["The product editor is open"],
        "data_requirements": [],
    }

    for acquisition_filters in (
        {"Name": "Selene Yoga Hoodie"},
        {"Type": "Configurable Product"},
    ):
        with pytest.raises(ValueError, match="acquisition_filters"):
            WorkerSpec.model_validate({
                **common,
                "strategy": {
                    "approach": "Search the product grid.",
                    "acquisition_filters": acquisition_filters,
                },
            })
