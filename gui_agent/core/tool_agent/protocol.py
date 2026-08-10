"""OpenAI-compatible tool protocol helpers used by Master and Worker."""

from __future__ import annotations

import base64
import json
import re
from copy import deepcopy
from typing import Any, Literal

from langchain_core.messages import HumanMessage
from pydantic import BaseModel, ConfigDict, Field

from gui_agent.core.tool_agent.contracts import DynamicActionSpec


class ProtocolError(RuntimeError):
    pass


class CompleteWorkerArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    result_ref: str
    evidence: list[str] = Field(default_factory=list)


class FailWorkerArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str


class RequestActionPatchArgs(BaseModel):
    """One frame-driven addition selected from the runtime capability registry."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    capability: Literal["tap", "scroll", "select_option"]
    description: str
    option_text: str = Field(
        default="",
        description=(
            "Exact visible option label for select_option when the subgoal determines it; "
            "leave empty for tap/scroll or when the Worker must choose the label later."
        ),
    )
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
    "select_option": {
        "type": "object",
        "properties": {
            "x": {"type": "number", "minimum": 0, "maximum": 999},
            "y": {"type": "number", "minimum": 0, "maximum": 999},
            "text": {
                "type": "string",
                "minLength": 1,
                "description": "Visible option label to select.",
            },
            "description": {"type": "string"},
        },
        "required": ["x", "y", "text"],
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


def worker_action_floor() -> list[DynamicActionSpec]:
    """GUI affordances that remain available when the Master missed a frame detail."""
    return [
        DynamicActionSpec(
            name="runtime_tap_visible",
            capability="tap",
            description="Tap one visible control that advances the current Worker goal.",
            exposed_args=["description"],
        ),
        DynamicActionSpec(
            name="runtime_scroll_visible",
            capability="scroll",
            description="Scroll a visible region to reveal content needed by the current Worker goal.",
            exposed_args=["direction", "amount", "target_area", "description"],
        ),
    ]


def dynamic_worker_tools(actions: list[DynamicActionSpec]) -> list[dict[str, Any]]:
    tools = [dynamic_action_tool(item) for item in actions]
    tools.extend(
        [
            model_tool(
                "request_action_patch",
                (
                    "Add one missing frame-driven GUI action from the registered capability set, "
                    "then reason again on the same screenshot. This does not execute a GUI action."
                ),
                RequestActionPatchArgs,
            ),
            model_tool(
                "complete",
                "Complete this worker using a ResultRef produced by a runtime action.",
                CompleteWorkerArgs,
            ),
            model_tool("fail", "Stop this worker with an explicit reason.", FailWorkerArgs),
        ]
    )
    return tools


def materialize_action_patch(args: RequestActionPatchArgs) -> DynamicActionSpec:
    """Materialize semantics through registry-owned capability parameter contracts."""
    fixed_args: dict[str, Any] = {}
    exposed_args: list[str] = []
    if args.capability == "scroll":
        exposed_args = ["direction", "amount", "target_area", "description"]
    elif args.capability == "select_option":
        if args.option_text.strip():
            fixed_args["text"] = args.option_text.strip()
        else:
            exposed_args.append("text")
    return DynamicActionSpec(
        name=args.name,
        capability=args.capability,
        description=args.description,
        fixed_args=fixed_args,
        exposed_args=exposed_args,
    )


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


def response_usage(response: Any) -> dict[str, int]:
    """Normalize token metadata emitted by OpenAI-compatible chat endpoints."""
    usage = getattr(response, "usage_metadata", None) or {}
    response_metadata = getattr(response, "response_metadata", None) or {}
    fallback = response_metadata.get("token_usage") or response_metadata.get("usage") or {}
    input_tokens = int(
        usage.get("input_tokens")
        or fallback.get("prompt_tokens")
        or fallback.get("input_tokens")
        or 0
    )
    output_tokens = int(
        usage.get("output_tokens")
        or fallback.get("completion_tokens")
        or fallback.get("output_tokens")
        or 0
    )
    total_tokens = int(
        usage.get("total_tokens")
        or fallback.get("total_tokens")
        or input_tokens + output_tokens
    )
    return {
        "input": input_tokens,
        "output": output_tokens,
        "total": total_tokens,
    }


def diagnostic_prompt_reports(
    label: str,
    messages: list[Any],
    response: Any,
    *,
    parsed: dict[str, Any] | None = None,
    schema: str = "",
) -> list[dict[str, Any]]:
    """Create report-only model I/O records without persisting image base64."""

    roles: list[dict[str, Any]] = []
    for message in messages:
        role = str(getattr(message, "type", None) or message.__class__.__name__).lower()
        role = {
            "systemmessage": "system",
            "humanmessage": "human",
            "aimessage": "assistant",
            "toolmessage": "tool",
            "ai": "assistant",
        }.get(role, role)
        content = getattr(message, "content", "")
        raw_parts = content if isinstance(content, list) else [{"type": "text", "text": str(content)}]
        parts: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_parts, 1):
            if not isinstance(raw, dict):
                text = str(raw)
                parts.append({
                    "label": f"part_{index}",
                    "source_type": "runtime_message",
                    "source": label,
                    "type": "text",
                    "text": text,
                    "chars": len(text),
                })
                continue
            part_type = str(raw.get("type") or "text")
            if part_type in {"image", "image_url"}:
                text = "[image_url omitted from diagnostic log]"
                parts.append({
                    "label": "screenshot",
                    "source_type": "runtime_image",
                    "source": label,
                    "type": "image",
                    "text": text,
                    "chars": len(text),
                })
                continue
            text = str(raw.get("text") or raw.get("content") or "")
            if not text:
                text = json.dumps(
                    {key: value for key, value in raw.items() if key not in {"image", "image_url"}},
                    ensure_ascii=False,
                    default=str,
                )
            parts.append({
                "label": f"part_{index}",
                "source_type": "runtime_message",
                "source": label,
                "type": "text",
                "text": text,
                "chars": len(text),
            })
        tool_calls = getattr(message, "tool_calls", None) or []
        if tool_calls:
            text = json.dumps(tool_calls, ensure_ascii=False, indent=2, default=str)
            parts.append({
                "label": "tool_calls",
                "source_type": "runtime_tool_calls",
                "source": label,
                "type": "text",
                "text": text,
                "chars": len(text),
            })
        roles.append({"role": role, "parts": parts})

    raw_output = message_text(getattr(response, "content", ""))
    response_calls = getattr(response, "tool_calls", None) or []
    if response_calls:
        rendered_calls = json.dumps(response_calls, ensure_ascii=False, indent=2, default=str)
        raw_output = f"{raw_output}\n\nTool calls:\n{rendered_calls}".strip()
    return [
        {"kind": "prompt_snapshot", "label": label, "roles": roles},
        {
            "kind": "llm_output",
            "label": label,
            "schema": schema,
            "mode": "tool_call" if response_calls else "text",
            "raw_output": raw_output,
            "parsed": parsed,
            "chars": len(raw_output),
        },
    ]


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
