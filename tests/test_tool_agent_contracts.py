from __future__ import annotations

import pytest
from jsonschema import ValidationError, validate

from gui_agent.core.tool_agent.contracts import DataRequirement, DynamicActionSpec
from gui_agent.core.tool_agent.protocol import dynamic_action_tool


def test_dynamic_action_exposes_only_worker_decisions() -> None:
    action = DynamicActionSpec(
        name="reveal_more_records",
        capability="scroll",
        description="Reveal the next visual window",
        fixed_args={"direction": "down", "target_area": "main_content"},
        exposed_args=["amount"],
    )

    parameters = dynamic_action_tool(action)["function"]["parameters"]

    assert set(parameters["properties"]) == {"amount"}
    validate(instance={"amount": "medium"}, schema=parameters)
    with pytest.raises(ValidationError):
        validate(instance={"direction": "up"}, schema=parameters)


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
