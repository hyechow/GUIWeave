"""OpenAI-compatible tool protocol helpers used by Master and Worker."""

from __future__ import annotations

import base64
import json
import re
from copy import deepcopy
from typing import Any, Literal

from jsonschema import validate
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field

from gui_agent.core.tool_agent.contracts import (
    DynamicActionSpec,
    ToolActionCapability,
)


MAX_ORDERED_ACTIONS = 5


class ProtocolError(RuntimeError):
    pass


class CompleteWorkerArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    collection_ref: str = Field(
        default="",
        description=(
            "Complete CollectionRef for a collector Worker. Leave empty for an "
            "operator Worker after its target UI state is confirmed."
        ),
    )
    evidence: list[str] = Field(default_factory=list)


class CompleteReadyWorkerArgs(BaseModel):
    """Completion evidence when Runtime owns any required data reference."""

    model_config = ConfigDict(extra="forbid")
    evidence: list[str] = Field(default_factory=list)


class FailWorkerArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str


class RequestActionPatchArgs(BaseModel):
    """One frame-driven addition selected from the runtime capability registry."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    capability: ToolActionCapability
    description: str
    input_text: str = Field(
        default="",
        description=(
            "Exact goal-determined text for type when known; leave empty when the "
            "Worker must choose it from current task context."
        ),
    )
    option_text: str = Field(
        default="",
        description=(
            "Exact visible option label for select_option when the subgoal determines it; "
            "leave empty for tap/scroll or when the Worker must choose the label later."
        ),
    )
    url: str = Field(
        default="",
        description=(
            "Exact task- or knowledge-provided URL for open_url when known; leave "
            "empty when the Worker must choose it from its current context."
        ),
    )
    reason: str


_CAPABILITY_SCHEMAS: dict[str, dict[str, Any]] = {
    "tap": {
        "type": "object",
        "properties": {
            "x": {"type": "number", "minimum": 0, "maximum": 999},
            "y": {"type": "number", "minimum": 0, "maximum": 999},
            "description": {
                "type": "string",
                "minLength": 5,
                "maxLength": 240,
                "description": (
                    "One atomic visible target: include its visible name, control type, "
                    "and screen region; do not include later actions."
                ),
            },
        },
        "required": ["x", "y"],
        "additionalProperties": False,
    },
    "type": {
        "type": "object",
        "properties": {
            "x": {"type": "number", "minimum": 0, "maximum": 999},
            "y": {"type": "number", "minimum": 0, "maximum": 999},
            "text": {
                "type": "string",
                "minLength": 1,
                "description": "Text to enter into the visible input control.",
            },
            "description": {
                "type": "string",
                "minLength": 5,
                "maxLength": 240,
                "description": (
                    "One atomic visible input target: include its visible name and screen "
                    "region; do not include later actions."
                ),
            },
        },
        "required": ["x", "y", "text"],
        "additionalProperties": False,
    },
    "clear_text": {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
    "press_enter": {
        "type": "object",
        "properties": {},
        "required": [],
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
            "description": {
                "type": "string",
                "minLength": 5,
                "maxLength": 240,
                "description": "Describe only the current scroll region and purpose.",
            },
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
            "description": {
                "type": "string",
                "minLength": 5,
                "maxLength": 240,
                "description": "Describe only the current visible choice control.",
            },
        },
        "required": ["x", "y", "text"],
        "additionalProperties": False,
    },
    "open_url": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "minLength": 1,
                "maxLength": 2048,
                "description": "Task- or knowledge-provided URL to open in the current tab.",
            },
        },
        "required": ["url"],
        "additionalProperties": False,
    },
    "back": {
        "type": "object",
        "properties": {},
        "required": [],
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
        DynamicActionSpec(
            name="runtime_type_visible",
            capability="type",
            description=(
                "Enter task- or observation-determined text into one visible input control. "
                "Use this when recovery needs a value different from a fixed-input action."
            ),
            exposed_args=["text", "description"],
        ),
        DynamicActionSpec(
            name="runtime_clear_focused",
            capability="clear_text",
            description="Clear the currently focused visible text input.",
        ),
        DynamicActionSpec(
            name="runtime_press_enter",
            capability="press_enter",
            description="Press Enter to submit or confirm the currently focused control.",
        ),
        DynamicActionSpec(
            name="runtime_select_visible",
            capability="select_option",
            description="Choose one named option from a visible choice control.",
            exposed_args=["text", "description"],
        ),
        DynamicActionSpec(
            name="runtime_open_url",
            capability="open_url",
            description=(
                "Open an exact absolute URL or route copied from the task or application "
                "knowledge in the current browser tab. Runtime rejects inferred routes."
            ),
        ),
        DynamicActionSpec(
            name="runtime_browser_back",
            capability="back",
            description="Go back once in the current browser tab's history.",
        ),
    ]


_WORKER_STATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Compact semantic state paired with this atomic action.",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["exploring", "collecting", "completed", "failed"],
        },
        "summary": {"type": "string", "maxLength": 320},
        "next_instruction": {"type": "string", "maxLength": 240},
    },
    "required": ["status", "summary", "next_instruction"],
    "additionalProperties": False,
}


def _with_worker_state(tool: dict[str, Any]) -> dict[str, Any]:
    """Add one compact common state carrier to a provider-facing tool schema."""
    wrapped = deepcopy(tool)
    parameters = wrapped["function"]["parameters"]
    parameters["properties"] = {
        "state": deepcopy(_WORKER_STATE_SCHEMA),
        **dict(parameters.get("properties") or {}),
    }
    parameters["required"] = [
        "state",
        *[name for name in parameters.get("required") or [] if name != "state"],
    ]
    return wrapped


def dynamic_worker_tools(
    actions: list[DynamicActionSpec],
    *,
    completion_mode: Literal[
        "legacy", "unavailable", "operator", "collector"
    ] = "legacy",
    action_envelope: bool = False,
) -> list[dict[str, Any]]:
    tools = (
        [_with_worker_state(dynamic_action_envelope_tool(actions))]
        if action_envelope
        else [_with_worker_state(dynamic_action_tool(item)) for item in actions]
    )
    tools.append(_with_worker_state(model_tool(
        "request_action_patch",
        (
            "Add one missing frame-driven GUI action from the registered capability set, "
            "then reason again on the same screenshot. This does not execute a GUI action."
        ),
        RequestActionPatchArgs,
    )))
    if completion_mode != "unavailable":
        complete_model = (
            CompleteWorkerArgs
            if completion_mode == "legacy"
            else CompleteReadyWorkerArgs
        )
        description = (
            "Complete this GUI Worker. A collector must provide its complete "
            "CollectionRef; an operator leaves collection_ref empty."
            if completion_mode == "legacy"
            else "Complete this operator after its target UI state is visibly confirmed."
            if completion_mode == "operator"
            else (
                "Complete this collector using the valid complete CollectionRef already "
                "bound by Runtime for the current frame."
            )
        )
        tools.append(_with_worker_state(model_tool(
            "complete",
            description,
            complete_model,
        )))
    tools.append(_with_worker_state(model_tool(
        "fail", "Stop this worker with an explicit reason.", FailWorkerArgs
    )))
    return tools


def dynamic_action_envelope_tool(
    actions: list[DynamicActionSpec],
) -> dict[str, Any]:
    """Wrap unchanged dynamic actions in one ordered Worker decision."""

    variants = [
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "const": action.name},
                "args": dynamic_action_tool(action)["function"]["parameters"],
            },
            "required": ["name", "args"],
            "additionalProperties": False,
        }
        for action in actions
    ]
    return function_tool(
        "continue_with_actions",
        (
            f"Continue with one to {MAX_ORDERED_ACTIONS} ordered actions on already-visible targets. "
            "Later actions must not depend on newly revealed UI. Put surface-changing "
            "actions last, except a tap that focuses the input used by following "
            "clear/type actions. Runtime may discard a stale suffix."
        ),
        {
            "type": "object",
            "properties": {
                "actions": {
                    "type": "array",
                    "items": {"oneOf": variants},
                    "minItems": 1,
                    "maxItems": MAX_ORDERED_ACTIONS,
                },
            },
            "required": ["actions"],
            "additionalProperties": False,
        },
    )


def materialize_action_patch(args: RequestActionPatchArgs) -> DynamicActionSpec:
    """Materialize semantics through registry-owned capability parameter contracts."""
    fixed_args: dict[str, Any] = {}
    exposed_args: list[str] = []
    if args.capability == "scroll":
        exposed_args = ["direction", "amount", "target_area", "description"]
    elif args.capability == "type":
        if args.input_text.strip():
            fixed_args["text"] = args.input_text.strip()
        else:
            exposed_args.append("text")
    elif args.capability == "select_option":
        if args.option_text.strip():
            fixed_args["text"] = args.option_text.strip()
        else:
            exposed_args.append("text")
    elif args.capability == "open_url":
        if args.url.strip():
            fixed_args["url"] = args.url.strip()
        else:
            exposed_args.append("url")
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
    if "description" in exposed and "description" not in schema["required"]:
        schema["required"].append("description")
    description = action.description
    if action.fixed_args:
        description += (
            " Runtime has fixed this tool's non-spatial input values; it cannot execute a "
            "different recovery value. Choose a value-bearing baseline tool when the value "
            "must change."
        )
    return function_tool(action.name, description, schema)


def capability_parameters(capability: str) -> dict[str, Any]:
    try:
        return deepcopy(_CAPABILITY_SCHEMAS[capability])
    except KeyError as exc:
        raise ValueError(f"unknown capability {capability!r}") from exc


def validate_dynamic_action_spec(action: DynamicActionSpec) -> None:
    """Validate that one business action can satisfy its capability contract.

    This check is shared by static Master review and live Worker dispatch so an
    impossible dynamic tool schema is rejected before any GUI execution begins.
    """

    parameters = capability_parameters(action.capability)
    properties = parameters.get("properties") or {}
    unknown_fixed = set(action.fixed_args).difference(properties)
    if unknown_fixed:
        raise ValueError(f"{action.name}: unknown fixed args {sorted(unknown_fixed)}")
    unknown_bound = set(action.input_args).difference(properties)
    if unknown_bound:
        raise ValueError(
            f"{action.name}: unknown runtime-bound args {sorted(unknown_bound)}"
        )
    for name, value in action.fixed_args.items():
        validate(instance=value, schema=properties[name])
    missing_required = (
        set(parameters.get("required") or [])
        .difference(action.fixed_args)
        .difference(action.input_args)
        .difference(action.exposed_args)
    )
    if missing_required:
        raise ValueError(
            f"{action.name}: required args are neither fixed, runtime-bound, nor exposed: "
            f"{sorted(missing_required)}"
        )
    dynamic_action_tool(action)


def image_message(text: str, png: bytes) -> HumanMessage:
    encoded = base64.b64encode(png).decode("ascii")
    return HumanMessage(
        content=[
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
        ]
    )


def cacheable_system_message(
    text: str,
    *,
    enabled: bool,
    suffix: str = "",
) -> SystemMessage:
    """Mark one stable system prefix for providers with explicit prompt caching."""

    if not enabled:
        return SystemMessage(content=text + suffix)
    content: list[dict[str, Any]] = [{
        "type": "text",
        "text": text,
        "cache_control": {"type": "ephemeral"},
    }]
    if suffix:
        content.append({"type": "text", "text": suffix})
    return SystemMessage(content=content)


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
    cached_input = 0
    for details in (
        usage.get("input_token_details"),
        fallback.get("prompt_tokens_details"),
        fallback.get("input_token_details"),
        usage,
        fallback,
    ):
        if isinstance(details, dict):
            cached_input = int(
                details.get("cache_read")
                or details.get("cached_tokens")
                or details.get("cache_read_input_tokens")
                or 0
            )
        if cached_input:
            break
    cached_input = max(0, min(input_tokens, cached_input))
    return {
        "input": input_tokens,
        "output": output_tokens,
        "total": total_tokens,
        "cached_input": cached_input,
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
    raw_args = call.get("args") or {}
    if isinstance(raw_args, str):
        try:
            raw_args = json.loads(raw_args)
        except json.JSONDecodeError as exc:
            raise ProtocolError(f"tool arguments are invalid JSON: {exc}") from exc
    if not isinstance(raw_args, dict):
        raise ProtocolError("tool arguments must be an object")
    return {
        "id": str(call.get("id") or "tool-call"),
        "name": str(call.get("name") or ""),
        "args": dict(raw_args),
    }


def normalize_action_arguments(args: dict[str, Any]) -> dict[str, Any]:
    """Repair common provider coordinate encodings before strict validation.

    The model-facing schema remains the simple canonical form. Some multimodal
    endpoints nevertheless return a point as ``[x, y]`` in both coordinate fields,
    or under a ``point``/``coordinates`` alias. These are losslessly decodable
    transport variants, so rejecting them and paying for another LLM turn would be
    wasteful. Ambiguous or non-numeric values are deliberately left for validation.
    """
    normalized = dict(args)
    point = None
    for alias in ("point", "coordinate", "coordinates"):
        candidate = normalized.pop(alias, None)
        if point is None and isinstance(candidate, (list, tuple)) and len(candidate) == 2:
            point = candidate
    if point is not None:
        normalized.setdefault("x", point[0])
        normalized.setdefault("y", point[1])

    for coordinate in ("x", "y"):
        value = normalized.get(coordinate)
        if not isinstance(value, str):
            continue
        stripped = value.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            try:
                decoded = json.loads(stripped)
            except json.JSONDecodeError:
                pass
            else:
                normalized[coordinate] = decoded
                continue
        try:
            number = float(stripped)
        except ValueError:
            continue
        normalized[coordinate] = int(number) if number.is_integer() else number

    x = normalized.get("x")
    y = normalized.get("y")
    if isinstance(x, (list, tuple)):
        if len(x) == 2:
            normalized["x"] = x[0]
            if not isinstance(y, (int, float)) or isinstance(y, bool):
                normalized["y"] = x[1]
        elif len(x) == 1:
            normalized["x"] = x[0]
    y = normalized.get("y")
    if isinstance(y, (list, tuple)):
        if len(y) == 2:
            if not isinstance(normalized.get("x"), (int, float)):
                normalized["x"] = y[0]
            normalized["y"] = y[1]
        elif len(y) == 1:
            normalized["y"] = y[0]
    return normalized
