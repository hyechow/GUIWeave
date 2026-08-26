"""OpenAI-compatible tool protocol helpers used by Master and Worker."""

from __future__ import annotations

import base64
from io import BytesIO
import json
import re
from dataclasses import dataclass
from copy import deepcopy
from typing import Any, Literal

from jsonschema import validate
from langchain_core.messages import HumanMessage, SystemMessage
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

from gui_agent.core.tool_agent.contracts import (
    DynamicActionSpec,
    MaterializedFrame,
    RuntimeInputBinding,
    WorkerSpec,
    WorkerInputBinding,
)


MAX_ORDERED_ACTIONS = 5


@dataclass(frozen=True)
class WorkerFrameTools:
    allowed_actions: list[DynamicActionSpec]
    completion_mode: Literal["unavailable", "operator", "collector"]


def worker_frame_tools(
    spec: WorkerSpec,
    actions: list[DynamicActionSpec],
    frame: MaterializedFrame,
    *,
    attempted_action: bool = False,
) -> WorkerFrameTools:
    """Expose tools from mechanical frame readiness; make no task judgment."""
    if frame.readiness != "ready":
        available = [] if attempted_action else [
            action for action in actions
            if action.capability in {"open_url", "back", "home", "app_switch", "launch_app"}
        ]
        return WorkerFrameTools(available, "unavailable")
    return WorkerFrameTools(actions, spec.profile)


_TERMINAL_TOOL_BY_STATE = {"completed": "complete", "failed": "report_blocked"}
_INPUT_TARGETS = {
    "text_input": ("type", "text"),
    "choice": ("select_option", "text"),
    "url": ("open_url", "url"),
    "application": ("launch_app", "app"),
}
_NUMBER = r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?"
_ENCODED_COORD_PAIR = re.compile(
    rf'(?P<head>"(?P<prefix>to_)?x"\s*:\s*{_NUMBER}),\s*'
    rf'(?P<y>{_NUMBER})(?=\s*[,}}])'
)


class ProtocolError(RuntimeError):
    pass


def validate_actor_tool_state(tool: str, state_status: str) -> None:
    """Keep terminal state claims aligned with the selected protocol tool."""

    if (
        state_status in _TERMINAL_TOOL_BY_STATE
        or tool in _TERMINAL_TOOL_BY_STATE.values()
    ) and _TERMINAL_TOOL_BY_STATE.get(state_status) != tool:
        raise ProtocolError(
            f"terminal state/tool mismatch: state.status={state_status!r}, tool={tool!r}"
        )


class CompleteReadyWorkerArgs(BaseModel):
    """Completion evidence when Runtime owns any required data reference.

    ``rows`` carries records the Worker read from surfaces perception could not
    extract; Runtime validates and accumulates them before binding the collection.
    """

    model_config = ConfigDict(extra="forbid")
    evidence: list[str] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)


class FailWorkerArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str


_COORD = {"type": "number", "minimum": 0, "maximum": 999}
_OPTIONAL_COORD = {**_COORD, "type": ["number", "null"]}


def _description(text: str) -> dict[str, Any]:
    return {"type": "string", "minLength": 5, "maxLength": 240, "description": text}


def _args(
    properties: dict[str, Any] | None = None,
    required: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties or {},
        "required": list(required),
        "additionalProperties": False,
    }


_EMPTY_ARGS = _args()
_CAPABILITY_SCHEMAS: dict[str, dict[str, Any]] = {
    "tap": _args({
        "x": _COORD, "y": _COORD,
        "description": _description(
            "One atomic visible target: include its visible name, control type, and "
            "screen region; use a point safely inside its tappable interior rather "
            "than on its outline or an overlapping viewport edge; do not include "
            "later actions."
        ),
    }, ("x", "y")),
    "type": _args({
        "x": _COORD, "y": _COORD,
        "text": {"type": "string", "minLength": 1,
                 "description": "Text to enter into the visible input control."},
        "description": _description(
            "One atomic visible input target: include its visible name and screen "
            "region; do not include later actions."
        ),
    }, ("x", "y", "text")),
    "clear_text": _EMPTY_ARGS,
    "press_enter": _EMPTY_ARGS,
    "scroll": _args({
        "direction": {
            "type": "string",
            "enum": ["up", "down", "left", "right"],
            "description": (
                "Content-traversal direction, not finger motion: down reveals content "
                "below and moves it upward; up reveals content above and moves it downward."
            ),
        },
        "amount": {"type": "string", "enum": ["small", "medium", "large"],
                   "default": "medium"},
        "target_area": {"type": "string", "enum": [
            "main_content", "left_panel", "right_panel", "top_content", "bottom_content",
        ], "default": "main_content"},
        "x": _OPTIONAL_COORD, "y": _OPTIONAL_COORD,
        "description": _description("Describe only the current scroll region and purpose."),
    }, ("direction",)),
    "drag": _args({
        "x": _COORD, "y": _COORD, "to_x": _COORD, "to_y": _COORD,
        "duration_ms": {"type": "integer", "minimum": 50, "maximum": 5000},
        "description": _description(
            "Describe the visible object and its exact drag destination."
        ),
    }, ("x", "y", "to_x", "to_y")),
    "long_press": _args({
        "x": _COORD, "y": _COORD,
        "duration_ms": {"type": "integer", "minimum": 300, "maximum": 3000},
        "description": _description("Describe the one visible control or item to hold."),
    }, ("x", "y")),
    "select_option": _args({
        "x": _COORD, "y": _COORD,
        "text": {"type": "string", "minLength": 1,
                 "description": "Visible option label to select."},
        "description": _description("Describe only the current visible choice control."),
    }, ("x", "y", "text")),
    "open_url": _args({"url": {
        "type": "string", "minLength": 1, "maxLength": 2048,
        "description": "Absolute HTTP(S) URL to open in the current tab. This browser-level "
                       "navigation replaces the current document and is not blocked by its "
                       "dialogs or overlays.",
    }}, ("url",)),
    "back": _EMPTY_ARGS,
    "home": _EMPTY_ARGS,
    "app_switch": _EMPTY_ARGS,
    "launch_app": _args({"app": {
        "type": "string", "minLength": 1, "maxLength": 120,
        "description": "Exact installed application name exposed by Runtime.",
    }}, ("app",)),
    "ask_user": _args({"question": {
        "type": "string", "minLength": 5, "maxLength": 500,
        "description": (
            "One concrete question whose answer supplies missing user-owned information "
            "required by the immutable task."
        ),
    }}, ("question",)),
}

_CAPABILITY_DESCRIPTIONS = {
    "tap": "Tap one visible control that advances the current approach.",
    "type": (
        "Replace the value of one visible input control. Use it directly to reformulate "
        "a visible query; no prior tap or clear action is required."
    ),
    "clear_text": "Clear the currently focused text control.",
    "press_enter": "Press Enter for the currently focused control.",
    "scroll": (
        "Traverse an ordinary visible collection or reveal a target outside the viewport. "
        "Never use this on a relevance-ordered web/search result page to hunt for later "
        "results; leading results decide query quality, so reformulate or report instead."
    ),
    "drag": "Drag one visible object to one visible destination.",
    "long_press": "Long-press one visible control or item.",
    "select_option": "Select one visible option from one visible choice control.",
    "open_url": (
        "Replace the current browser document with an absolute HTTP(S) URL that executes "
        "the current approach; current-page dialogs and overlays do not block this action."
    ),
    "back": "Navigate back once within the current execution path.",
    "home": "Return to the platform home surface.",
    "app_switch": "Open the platform application switcher.",
    "launch_app": "Launch one exact Runtime-provided installed application.",
    "ask_user": (
        "Ask the authoritative user for one missing value that materially determines the "
        "next task action. Do not ask for UI instructions, strategy, or already established "
        "facts. A descriptive role such as the user's usual, dedicated, preferred, or "
        "appropriate destination is not an exact identifier. If task context, active memory, "
        "and the bound application do not resolve that role to one exact value, ask before "
        "creating, typing, or selecting a guessed value. Authorization to mutate does not "
        "authorize inventing an identifier. Runtime returns the answer as task-lifetime "
        "Evidence on the next decision."
    ),
}

def function_tool(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": parameters},
    }


def model_tool(name: str, description: str, model: type[BaseModel]) -> dict[str, Any]:
    return function_tool(name, description, model.model_json_schema())


def worker_attempt_contract(
    spec: WorkerSpec,
    *,
    attempted_action: bool = False,
    current_element: str | None = None,
) -> str:
    """Serialize the binding attempt beside each current frame.

    ``current_element`` is the worker-visible locating hint for the current
    consume="each" plan element (Runtime-computed from the shared cursor). The
    bound values themselves stay private; without this hint a Worker iterating
    an array plan cannot know which record to locate on screen.
    """

    payload = {
        "approach": spec.strategy.approach,
        "phase": "continue" if attempted_action else "start",
        **spec.model_dump(mode="json", exclude={"input_refs", "strategy"}),
        "input_names": sorted(spec.input_refs),
    }
    if current_element:
        payload["current_element"] = current_element
    profile_rules = (
        "Collector rules for this attempt:\n"
        "- Form recall queries from natural equivalents and distinctive terms. Normalized "
        "filter values remain validation predicates; they need not appear literally or "
        "adjacent in query text.\n"
        "- When a requested date is relative to the authoritative task clock, use its "
        "natural relative-date term in the task language for recall rather than an ISO or "
        "long-form calendar literal; keep the exact date only for validation.\n"
        "- When a visible query needs no autocomplete selection, batch its `type` and "
        "`press_enter` in the same decision; do not spend a separate turn submitting it.\n"
        "- A relevance-ordered web/search page's leading visible titles assess one query. "
        "If they mismatch the goal, never scroll or paginate for later results: directly "
        "replace the visible query and submit, or report after materially different queries "
        "already failed.\n"
        "- Treat autocomplete as pending; commit a suggestion only when its identity "
        "matches the requested scope. `empty_authoritative = false` is a recall miss, "
        "not a completed empty result.\n"
        "- Recall candidates without changing semantic predicates, let Runtime validate "
        "requirement scope, then collect only scope-matched evidence.\n"
        "- You own the exhaustiveness judgment: narrow the scope with an exact filter or "
        "search first, then traverse; treat the collection as complete only when your own "
        "evidence says nothing remains (the filtered list fits the viewport, or further "
        "scrolling yields no new rows). Call `complete` on that judgment and state the "
        "evidence; Runtime never certifies completeness.\n"
        if spec.profile == "collector"
        else (
            "Operator rules for this attempt:\n"
            "- Treat multi-select and configuration goals as exact sets; validate any review "
            "surface before committing.\n"
            "- `candidate_set_state.status = exhausted` is guarded Runtime evidence; an "
            "initially empty or filtered selector is not exhausted.\n"
            "- Finish comparison evidence before mutation, skip excluded or already processed "
            "identities, and advance only through explicitly remaining candidates.\n"
            "- Durable facts resolve candidates across frames: a recorded no-match query or "
            "classified candidate stays resolved unless later evidence disproves it; never "
            "rerun that branch.\n"
            "- Never call `complete` while a visible final commit control for the requested "
            "mutation remains unactivated. Activate it and observe the next frame; readiness "
            "is not completion.\n"
            "- Observe the post-action frame. If the requested terminal mutation's commit "
            "just returned to a stable parent/source surface without error and durable "
            "facts name no unsatisfied identity, call `complete`; do not restart the "
            "mutation or rerun a resolved query. A preparatory scope/container commit is "
            "not terminal.\n"
        )
    )
    return (
        "## Current Worker attempt\n"
        "`approach` is binding for this attempt. Choose actions that execute it; do not "
        "continue an unrelated source, application, or mechanism visible in the frame. "
        "When `phase` is `start`, the first action's visible target or destination must "
        "identify the approach; the residue surface's usefulness for the goal is irrelevant. "
        "The goal and output contract are immutable. Runtime actions are generic "
        "capabilities, and named input bindings inject private Master-routed values.\n"
        + json.dumps(payload, ensure_ascii=False)
        + "\n"
        + profile_rules
    )


def dynamic_actor_tools(
    actions: list[DynamicActionSpec],
    *,
    completion_mode: Literal[
        "unavailable", "operator", "collector"
    ] = "operator",
    action_envelope: bool = False,
    max_ordered_actions: int = MAX_ORDERED_ACTIONS,
    allow_failure: bool = True,
) -> list[dict[str, Any]]:
    if action_envelope:
        interactions = [item for item in actions if item.capability == "ask_user"]
        batchable = [item for item in actions if item.capability != "ask_user"]
        tools = [
            *(
                dynamic_action_tool(
                    item,
                    include_state_target_ref=True,
                )
                for item in interactions
            ),
            *(
                [dynamic_action_envelope_tool(
                    batchable,
                    max_ordered_actions=max_ordered_actions,
                )]
                if batchable else []
            ),
        ]
    else:
        tools = [
            dynamic_action_tool(
                item,
                include_state_target_ref=True,
            )
            for item in actions
        ]
    if completion_mode != "unavailable":
        if completion_mode == "operator":
            description = (
                "Complete only when the authoritative State status is completed. With "
                "element-wise bindings, complete the current element so Runtime can advance "
                "its cursor; State is reinitialized for the next element."
            )
        else:
            description = (
                "Complete this collector only when authoritative State is completed and the "
                "Runtime-exposed collection output matches the requested scope."
            )
        tools.append(model_tool(
            "complete",
            description,
            CompleteReadyWorkerArgs,
        ))
    if allow_failure:
        tools.append(model_tool(
            "report_blocked",
            "Report concrete execution evidence that the current approach cannot "
            "continue. Strategy, not Worker, decides whether to replace the approach.",
            FailWorkerArgs,
        ))
    return tools


def dynamic_action_envelope_tool(
    actions: list[DynamicActionSpec],
    *,
    max_ordered_actions: int = MAX_ORDERED_ACTIONS,
) -> dict[str, Any]:
    """Wrap unchanged dynamic actions in one ordered Worker decision."""

    if (
        not isinstance(max_ordered_actions, int)
        or isinstance(max_ordered_actions, bool)
        or not 1 <= max_ordered_actions <= MAX_ORDERED_ACTIONS
    ):
        raise ValueError(f"max_ordered_actions must be in [1, {MAX_ORDERED_ACTIONS}]")
    variants = []
    for action in actions:
        tool = dynamic_action_tool(
            action,
            include_state_target_ref=True,
        )["function"]
        variants.append({
            "type": "object",
            "description": tool["description"],
            "properties": {
                "name": {"type": "string", "const": action.name},
                "args": tool["parameters"],
            },
            "required": ["name", "args"],
            "additionalProperties": False,
        })
    action_range = (
        "exactly one action"
        if max_ordered_actions == 1
        else f"one to {max_ordered_actions} ordered actions"
    )
    shared_description = (
        f"Continue with {action_range}; this tool never represents completion and its "
        "action list cannot be empty. If the goal is complete, call complete directly. "
        "All actions must form one immediate UI transaction; never mix discovery, reveal, "
        "or recovery with mutation. Later actions must not depend on newly revealed UI. "
        "Runtime settles each action and visually re-grounds the next target on a fresh "
        "screenshot. Scroll, drag, home, back, app switching, app launch, and direct "
        "navigation must be final. launch_app works directly from any current application; "
        "never prepend home or app_switch to it."
    )
    role_description = (
        " Choose only current-frame actions that advance State's unresolved difference. "
        "For a goal target, copy its unresolved frontier ref; never use a resolved ref."
    )
    return function_tool(
        "continue_with_actions",
        shared_description + role_description,
        {
            "type": "object",
            "properties": {
                "actions": {
                    "type": "array",
                    "items": {"oneOf": variants},
                    "minItems": 1,
                    "maxItems": max_ordered_actions,
                },
            },
            "required": ["actions"],
            "additionalProperties": False,
        },
    )


def dynamic_action_tool(
    action: DynamicActionSpec,
    *,
    include_state_target_ref: bool = False,
) -> dict[str, Any]:
    schema = deepcopy(_CAPABILITY_SCHEMAS[action.capability])
    properties = schema["properties"]
    exposed = set(action.exposed_args)
    unknown = exposed.difference(properties)
    if unknown:
        raise ValueError(f"{action.name}: unknown exposed args {sorted(unknown)}")
    properties = {name: value for name, value in properties.items() if name in exposed}
    if include_state_target_ref and action.capability in {
        "tap", "click", "type", "drag", "long_press", "select_option",
    }:
        properties["state_target_ref"] = {
            "type": ["string", "null"],
            "pattern": r"^[a-z][a-z0-9_]{0,79}$",
            "description": (
                "Copy the exact target_ref when this action spatially operates on a "
                "target in State.visible_targets.unresolved_frontier. Use null only "
                "for a navigation or interface control that State does not track as "
                "a goal target. This semantic binding never replaces x/y geometry."
            ),
        }
    schema["properties"] = properties
    schema["required"] = [name for name in schema.get("required", []) if name in exposed]
    if "description" in exposed and "description" not in schema["required"]:
        schema["required"].append("description")
    description = action.description
    if action.fixed_args:
        description += (
            " Runtime has fixed this tool's non-spatial input values; it cannot execute a "
            "different recovery value. Fail this strategy when that value must change."
        )
    return function_tool(action.name, description, schema)


def capability_parameters(capability: str) -> dict[str, Any]:
    try:
        return deepcopy(_CAPABILITY_SCHEMAS[capability])
    except KeyError as exc:
        raise ValueError(f"unknown capability {capability!r}") from exc


def generic_action_spec(capability: str) -> DynamicActionSpec:
    """Expose one adapter capability without asking Master or Strategy to enumerate it."""

    parameters = capability_parameters(capability)
    return DynamicActionSpec(
        name=capability,
        capability=capability,
        description=_CAPABILITY_DESCRIPTIONS[capability],
        exposed_args=list((parameters.get("properties") or {}).keys()),
    )


def input_binding_action(binding: WorkerInputBinding) -> DynamicActionSpec:
    """Lower one semantic input binding into a private-value Runtime action."""

    capability, argument = _INPUT_TARGETS[binding.target]
    properties = capability_parameters(capability).get("properties") or {}
    action = DynamicActionSpec(
        name=binding.name,
        capability=capability,
        description=binding.description,
        input_args={
            argument: RuntimeInputBinding(
                input=binding.input,
                path=binding.path,
            )
        },
        exposed_args=[name for name in properties if name != argument],
    )
    validate_dynamic_action_spec(action)
    return action


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


def _image_content(png: bytes, *, scale: float) -> dict[str, Any]:
    if not 0 < scale <= 1:
        raise ValueError("image scale must be in (0, 1]")
    if scale < 1:
        with Image.open(BytesIO(png)) as image:
            size = tuple(max(1, round(axis * scale)) for axis in image.size)
            resized = image.resize(size, Image.Resampling.LANCZOS)
            buffer = BytesIO()
            resized.save(buffer, format="PNG", optimize=True)
            png = buffer.getvalue()
    encoded = base64.b64encode(png).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}}


def image_message(text: str, png: bytes, *, scale: float = 1.0) -> HumanMessage:
    return HumanMessage(
        content=[
            {"type": "text", "text": text},
            _image_content(png, scale=scale),
        ]
    )


def frame_transition_message(
    text: str,
    previous_png: bytes,
    current_png: bytes,
    *,
    previous_frame_id: str,
    current_frame_id: str,
    previous_scale: float = 0.75,
    current_scale: float = 1.0,
) -> HumanMessage:
    """Carry the labeled image pair for one append-only State transition."""

    return HumanMessage(content=[
        {"type": "text", "text": text},
        {"type": "text", "text": f"previous_frame ({previous_frame_id})"},
        _image_content(previous_png, scale=previous_scale),
        {"type": "text", "text": f"current_frame ({current_frame_id})"},
        _image_content(current_png, scale=current_scale),
    ])


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


def json_actor_decision_instruction(tools: list[dict[str, Any]]) -> str:
    """Describe the same dynamic action contract without provider tool calling."""

    catalog = {
        function["name"]: {
            "description": function.get("description", ""),
            "parameters": function["parameters"],
        }
        for tool in tools
        if isinstance((function := tool.get("function")), dict)
    }
    return (
        "Decision transport: return only one JSON object with exactly two keys: "
        '`{"tool": "<exact action name>", "args": {<arguments>}}`. '
        "Do not emit a function/tool call, Markdown, commentary, or a JSON Schema. "
        "Choose exactly one listed action. The State role already ran; do not emit a "
        "`state` field. The outer args "
        "must satisfy the selected action's schema. "
        "Available contract:\n"
        + json.dumps(catalog, ensure_ascii=False, separators=(",", ":"))
    )


def bind_actor_decision_transport(
    llm: Any,
    tools: list[dict[str, Any]],
    *,
    protocol: str,
    bind_kwargs: dict[str, Any] | None = None,
) -> tuple[Any, str, str]:
    """Bind one provider transport while keeping the action contract unchanged."""

    kwargs = bind_kwargs or {}
    if protocol == "json":
        return (
            llm.bind(**kwargs),
            json_actor_decision_instruction(tools),
            "return exactly one JSON object with only tool and args",
        )
    if protocol == "tool_call":
        names = ", ".join(
            str(tool.get("function", {}).get("name") or "")
            for tool in tools
            if tool.get("function", {}).get("name")
        )
        return llm.bind_tools(
            tools,
            tool_choice="required",
            parallel_tool_calls=False,
            **kwargs,
        ), "", (
            f"emit exactly one required tool call named one of: {names}; "
            "batched GUI actions belong inside continue_with_actions.args.actions"
        )
    raise ValueError(f"unsupported Actor action protocol {protocol!r}")


def decode_actor_action(
    response: Any,
    *,
    protocol: str = "tool_call",
    tools: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], Any, list[dict[str, Any]]]:
    """Decode one transport response into normalized executable calls."""

    if protocol == "tool_call":
        call = exactly_one_tool_call(response)
    elif protocol == "json":
        value = parse_json_object(getattr(response, "content", ""))
        if not isinstance(value, dict) or not isinstance(value.get("args"), dict):
            raise ProtocolError("Actor decision JSON requires tool, object args, and optional rows")
        unknown = set(value) - {"tool", "args", "rows"}
        if unknown:
            raise ProtocolError(
                f"Actor decision JSON has unknown fields: {sorted(unknown)}"
            )
        if not isinstance(value.get("tool"), str) or not value["tool"].strip():
            raise ProtocolError("Actor decision JSON tool must be a non-empty string")
        rows = value.get("rows")
        if rows is not None and not isinstance(rows, list):
            raise ProtocolError("Worker decision JSON rows must be an array of records")
        call = {
            "id": "json-decision",
            "name": value["tool"].strip(),
            "args": value["args"],
            "rows": rows,
        }
    else:
        raise ValueError(f"unsupported Actor action protocol {protocol!r}")
    if call["name"] == "continue_with_actions":
        raw = call["args"].get("actions") or []
        raw = decode_ordered_actions(raw)
        if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
            raise ProtocolError("actions must be an ordered list of objects")
        raw = [
            item
            if "args" in item
            else {
                "name": item.get("name"),
                "args": {key: value for key, value in item.items() if key != "name"},
            }
            for item in raw
        ]
        calls = [{
            "name": str(item.get("name") or ""),
            "args": normalize_action_arguments(dict(item.get("args") or {})),
        } for item in raw]
        call["args"]["actions"] = calls
    else:
        call["args"] = normalize_action_arguments(call["args"])
        calls = [call]
    if tools is not None:
        tool = tools.get(call["name"])
        if tool is None:
            raise ProtocolError(f"unknown Actor tool {call['name']!r}")
        validate(instance=call["args"], schema=tool["function"]["parameters"])
    raw_state = call["args"].pop("state", None)
    if call["name"] == "continue_with_actions":
        call["args"]["actions"] = calls
    else:
        call = calls[0]
    return call, raw_state, calls


def decode_ordered_actions(value: object) -> object:
    """Decode a provider string with one common unkeyed coordinate repair."""

    return json.loads(_ENCODED_COORD_PAIR.sub(
        r'\g<head>,"\g<prefix>y":\g<y>', value,
    )) if isinstance(value, str) else value


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
