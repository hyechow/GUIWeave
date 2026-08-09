"""OpenAI-compatible tool protocol helpers used by Master and Worker."""

from __future__ import annotations

import base64
import json
import re
from copy import deepcopy
from typing import Any

from langchain_core.messages import HumanMessage
from pydantic import BaseModel, ConfigDict, Field

from gui_agent.core.tool_agent.contracts import DynamicActionSpec, WorkerSpec


class ProtocolError(RuntimeError):
    pass


class RunWorkerArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    spec: WorkerSpec


class FinishTaskArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    result_ref: str = Field(description="A ResultRef returned by a completed worker")


class FailTaskArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str


class CompleteWorkerArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    result_ref: str
    evidence: list[str] = Field(default_factory=list)


class FailWorkerArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str


_CAPABILITY_SCHEMAS: dict[str, dict[str, Any]] = {
    "tap": {
        "type": "object",
        "properties": {
            "x": {"type": "number", "minimum": 0, "maximum": 999},
            "y": {"type": "number", "minimum": 0, "maximum": 999},
            "description": {"type": "string"},
        },
        "required": ["x", "y"],
        "additionalProperties": False,
    },
    "scroll": {
        "type": "object",
        "properties": {
            "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
            "amount": {"type": "string", "enum": ["small", "medium", "large"], "default": "medium"},
            "target_area": {
                "type": "string",
                "enum": ["main_content", "left_panel", "right_panel", "top_content", "bottom_content"],
                "default": "main_content",
            },
            "x": {"type": ["number", "null"], "minimum": 0, "maximum": 999},
            "y": {"type": ["number", "null"], "minimum": 0, "maximum": 999},
            "description": {"type": "string"},
        },
        "required": ["direction"],
        "additionalProperties": False,
    },
    "python_transform": {
        "type": "object",
        "properties": {
            "data_ref": {"type": "string", "description": "CollectionRef to transform"},
        },
        "required": ["data_ref"],
        "additionalProperties": False,
    },
}


def function_tool(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": parameters},
    }


def model_tool(name: str, description: str, model: type[BaseModel]) -> dict[str, Any]:
    return function_tool(name, description, model.model_json_schema())


def master_tools() -> list[dict[str, Any]]:
    return [
        model_tool(
            "run_worker",
            "Start one dynamic agentic execution unit with its own internal observe/act loop.",
            RunWorkerArgs,
        ),
        model_tool(
            "finish_task",
            "Finish the task using an existing worker ResultRef. Never pass raw result values.",
            FinishTaskArgs,
        ),
        model_tool("fail_task", "Stop only when the task cannot be completed safely.", FailTaskArgs),
    ]


def dynamic_worker_tools(spec: WorkerSpec) -> list[dict[str, Any]]:
    tools = [dynamic_action_tool(item) for item in spec.actions]
    tools.extend(
        [
            model_tool(
                "complete",
                "Complete this worker using a ResultRef produced by a runtime action.",
                CompleteWorkerArgs,
            ),
            model_tool("fail", "Stop this worker with an explicit reason.", FailWorkerArgs),
        ]
    )
    return tools


def dynamic_action_tool(action: DynamicActionSpec) -> dict[str, Any]:
    schema = deepcopy(_CAPABILITY_SCHEMAS[action.capability])
    properties = schema["properties"]
    exposed = set(action.exposed_args)
    unknown = exposed.difference(properties)
    if unknown:
        raise ValueError(f"{action.name}: unknown exposed args {sorted(unknown)}")
    properties = {name: value for name, value in properties.items() if name in exposed}
    schema["properties"] = properties
    schema["required"] = [name for name in schema.get("required", []) if name in exposed]
    return function_tool(action.name, action.description, schema)


def capability_parameters(capability: str) -> dict[str, Any]:
    try:
        return deepcopy(_CAPABILITY_SCHEMAS[capability])
    except KeyError as exc:
        raise ValueError(f"unknown capability {capability!r}") from exc


def image_message(text: str, png: bytes) -> HumanMessage:
    encoded = base64.b64encode(png).decode("ascii")
    return HumanMessage(
        content=[
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
        ]
    )


def message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text") or "") if isinstance(item, dict) else str(item)
            for item in content
        )
    return str(content or "")


def parse_json_object(content: Any) -> dict[str, Any]:
    text = message_text(content).strip()
    if not text:
        raise ProtocolError("assistant content is empty")
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fenced is None:
            start, end = text.find("{"), text.rfind("}")
            if start < 0 or end <= start:
                raise ProtocolError("assistant content does not contain a JSON object")
            candidate = text[start : end + 1]
        else:
            candidate = fenced.group(1)
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise ProtocolError(f"invalid assistant JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ProtocolError("assistant content JSON must be an object")
    return value


def exactly_one_tool_call(response: Any) -> dict[str, Any]:
    calls = list(getattr(response, "tool_calls", None) or [])
    if len(calls) != 1:
        raise ProtocolError(f"expected exactly one tool call, got {len(calls)}")
    call = calls[0]
    return {
        "id": str(call.get("id") or "tool-call"),
        "name": str(call.get("name") or ""),
        "args": dict(call.get("args") or {}),
    }
