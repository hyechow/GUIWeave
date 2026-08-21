"""Typed contracts for the dynamic Master/Worker protocol."""

from __future__ import annotations

import re
from copy import deepcopy
from datetime import datetime
from typing import Any, Literal, TypeAlias, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

from gui_agent.core.tool_agent.filter_state import strip_contains_suffix


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


DataFieldType = Literal[
    "text", "text_list", "number", "money", "datetime", "boolean"
]

FailureKind: TypeAlias = Literal[
    "worker_blocked",
    "protocol_invalid",
    "action_contract_invalid",
    "platform_rejected",
    "action_not_allowed",
    "navigation_blocked",
    "budget_exhausted",
    "generator_invalid",
]

_ATOMIC_APPROACH_ACTION = re.compile(
    r"\b(?:tap|click|press|type|scroll|swipe|drag|select|open|navigate|navigation|launch|switch|"
    r"search|query|locate|find|extract|inspect|read)\b"
    r"|(?:点击|按下|输入|滚动|滑动|拖动|选择|打开|导航|启动|切换|搜索|查询|查找|定位|提取|检查|读取)",
    re.IGNORECASE,
)
_APPROACH_SEQUENCE = re.compile(
    r"\b(?:then|afterwards?|subsequently|next)\b|(?:然后|随后|接着|再去|再通过)",
    re.IGNORECASE,
)
_APPROACH_ACTION_SEQUENCE = re.compile(
    rf"(?:{_ATOMIC_APPROACH_ACTION.pattern}).{{0,80}}"
    rf"(?:\band\b|[,;]|并且?|再)\s*.{{0,20}}(?:{_ATOMIC_APPROACH_ACTION.pattern})",
    re.IGNORECASE,
)
_APPROACH_ACTION_COMMAND = re.compile(
    rf"^\s*(?:(?:direct|directly)\s+)?(?:{_ATOMIC_APPROACH_ACTION.pattern})",
    re.IGNORECASE,
)
_APPROACH_CAPABILITY_OR_URL = re.compile(
    r"\b(?:open_url|press_enter|clear_text|select_option|long_press|"
    r"app_switch|launch_app)\b|https?://",
    re.IGNORECASE,
)


def approach_atomic_action_count(approach: str) -> int:
    """Count adapter-scale action verbs in a Strategy approach."""

    return len(_ATOMIC_APPROACH_ACTION.findall(approach))


def approach_is_procedural(approach: str) -> bool:
    """Return whether an approach leaks actions, arguments, or a GUI procedure."""

    return bool(
        _APPROACH_ACTION_COMMAND.search(approach)
        or _APPROACH_CAPABILITY_OR_URL.search(approach)
        or _APPROACH_SEQUENCE.search(approach)
        or _APPROACH_ACTION_SEQUENCE.search(approach)
    )


class DataRequirement(StrictModel):
    """A semantic, provider-independent request for a logical collection."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    description: str
    target_label: str = Field(
        default="",
        description="Visible label/caption that identifies the data surface, when known.",
    )
    scope: Literal["collection"] = "collection"
    cardinality: Literal["one", "many"] = Field(
        default="many",
        description=(
            "Logical result cardinality. 'one' means the exact requested scope has "
            "at most one authoritative source record; 'many' requires collection coverage."
        ),
    )
    row_schema: dict[str, Any]
    field_sources: dict[str, str] = Field(
        default_factory=dict,
        description="Normalized output field to platform-visible source header.",
    )
    field_types: dict[str, DataFieldType] = Field(
        default_factory=dict,
        description=(
            "Source-value semantics used to normalize private runtime rows before "
            "they enter the DataStore. Datetimes become ISO 8601 strings; numbers "
            "and money become JSON numbers."
        ),
    )
    filters: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Immutable logical data restrictions keyed by normalized row field. "
            "They define the requested business scope independently of a physical "
            "attempt's broader or narrower UI acquisition literal."
        ),
    )
    coverage: Literal["complete", "first_match"] = "complete"

    @model_validator(mode="before")
    @classmethod
    def _expand_row_schema_shorthand(cls, data: object) -> object:
        """Expand the lossless ``{field: JSON-type}`` schema shorthand."""
        if not isinstance(data, dict):
            return data
        data = dict(data)
        schema = data.get("row_schema")
        if (
            isinstance(schema, dict)
            and "type" not in schema
            and "properties" not in schema
            and schema
            and all(value in {"string", "number", "integer", "boolean"} for value in schema.values())
        ):
            data["row_schema"] = {
                "type": "object",
                "properties": {key: {"type": value} for key, value in schema.items()},
                "required": list(schema),
                "additionalProperties": False,
            }
        return data

    @model_validator(mode="after")
    def _filters_are_collectable_fields(self) -> "DataRequirement":
        properties = set((self.row_schema.get("properties") or {}).keys())
        # "<field>_contains" is a semantic operator on <field>; the base field is
        # what must exist in row_schema (see filter_state.strip_contains_suffix).
        filter_fields = {strip_contains_suffix(field) for field in self.filters}
        unknown_filters = filter_fields.difference(properties)
        if unknown_filters:
            raise ValueError(
                "filter fields must be present in row_schema so logical row scope can "
                f"be verified: {sorted(unknown_filters)}"
            )
        optional_filters = filter_fields.difference(
            self.row_schema.get("required") or []
        )
        if optional_filters:
            raise ValueError(
                "filter fields must be required in row_schema so missing evidence cannot "
                f"satisfy logical scope: {sorted(optional_filters)}"
            )
        unknown_sources = set(self.field_sources).difference(properties)
        if unknown_sources:
            raise ValueError(
                f"field_sources keys are absent from row_schema: {sorted(unknown_sources)}"
            )
        unknown_types = set(self.field_types).difference(properties)
        if unknown_types:
            raise ValueError(
                f"field_types keys are absent from row_schema: {sorted(unknown_types)}"
            )
        expected_json_types = {
            "text": {"string"},
            "text_list": {"array"},
            "datetime": {"string"},
            "number": {"number", "integer"},
            "money": {"number", "integer"},
            "boolean": {"boolean"},
        }
        schema = deepcopy(self.row_schema)
        schema_properties = schema.get("properties") or {}
        for field, field_type in self.field_types.items():
            field_schema = schema_properties.get(field)
            json_type = field_schema.get("type") if isinstance(field_schema, dict) else None
            json_types = (
                {json_type}
                if isinstance(json_type, str)
                else set(json_type)
                if isinstance(json_type, list)
                and all(isinstance(item, str) for item in json_type)
                else set()
            )
            expected = expected_json_types[field_type]
            if not json_types.intersection(expected) or not json_types.issubset(
                expected | {"null"}
            ):
                raise ValueError(
                    f"field_types[{field!r}]={field_type!r} is incompatible with "
                    f"row_schema type {json_type!r}"
                )
            if field_type == "datetime":
                field_schema.setdefault("format", "date-time")
                constraint = self.filters.get(field)
                values = constraint.values() if isinstance(constraint, dict) else [constraint]
                for value in values:
                    try:
                        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                    except (TypeError, ValueError):
                        parsed = None
                    if constraint is not None and (
                        not isinstance(value, str)
                        or "T" not in value
                        or parsed is None
                        or parsed.tzinfo is None
                    ):
                        raise ValueError(
                            f"datetime filter {field!r} requires a full timezone-aware "
                            "ISO 8601 date-time"
                        )
            if field_type == "text_list" and (
                not isinstance(field_schema.get("items"), dict)
                or field_schema["items"].get("type") != "string"
            ):
                raise ValueError(
                    f"field_types[{field!r}]='text_list' requires string array items"
                )
        self.row_schema = schema
        return self


ToolActionCapability: TypeAlias = Literal[
    "tap",
    "type",
    "clear_text",
    "press_enter",
    "scroll",
    "drag",
    "long_press",
    "select_option",
    "open_url",
    "select_tab",
    "back",
    "home",
    "app_switch",
    "launch_app",
    "reveal_control",
]

# Viewport-normalized coordinate bounds for reveal_control. Standard spatial
# capabilities target the visible viewport [0, 1000); reveal targets frame
# controls whose positions legitimately exceed it when off-fold. Shared by the
# protocol schema, the runtime arg check, and the browser adapter validator.
REVEAL_COORD_MIN = -2000
REVEAL_COORD_MAX = 100_000

# Capabilities whose x/y/description the screenshot-owning Worker must supply.
_SPATIAL_ARG_CAPABILITIES = frozenset({
    "tap", "type", "scroll", "drag", "long_press", "select_option",
    "reveal_control",
})


def positioned_rect(control: dict[str, Any]) -> dict[str, Any] | None:
    """Return the control's rect when it carries numeric x/y, else None."""

    rect = control.get("rect") if isinstance(control, dict) else None
    if isinstance(rect, dict) and all(
        isinstance(rect.get(key), (int, float)) for key in ("x", "y")
    ):
        return rect
    return None


class RuntimeInputBinding(StrictModel):
    """Bind one action argument to a value inside a Runtime-owned ResultRef."""

    input: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    path: list[str | int] = Field(default_factory=list)


class WorkerInputBinding(StrictModel):
    """Bind one immutable Master-routed value to a semantic UI target."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    input: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    path: list[str | int] = Field(default_factory=list)
    target: Literal["text_input", "choice", "url", "application"]
    description: str = Field(min_length=1)


class DynamicActionSpec(StrictModel):
    """One business-named action bound to a small runtime capability."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    capability: ToolActionCapability
    description: str
    fixed_args: dict[str, Any] = Field(default_factory=dict)
    input_args: dict[str, RuntimeInputBinding] = Field(
        default_factory=dict,
        description=(
            "Action arguments resolved deterministically from Worker input_refs. "
            "The visual Worker chooses the action but never copies these values."
        ),
    )
    exposed_args: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _assign_spatial_args_to_worker(cls, data: object) -> object:
        """Keep screenshot-dependent coordinates out of the text-only Master.

        The Master defines the business action vocabulary but never sees Worker
        frames.  Treat any spatial values it emits as untrusted placeholders and
        canonicalize the action so the visual Worker must choose them instead.
        Tap coordinates remain required by the capability schema; scroll
        coordinates are optional anchors.
        """
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        fixed_args = dict(normalized.get("fixed_args") or {})
        input_args = dict(normalized.get("input_args") or {})
        exposed_args = list(normalized.get("exposed_args") or [])
        capability = normalized.get("capability")
        if capability in _SPATIAL_ARG_CAPABILITIES:
            # The screenshot-owning Worker supplies both the point and the
            # frame-specific target description used by enhanced grounding.
            for name in ("x", "y", "description"):
                fixed_args.pop(name, None)
                if name not in exposed_args:
                    exposed_args.append(name)
            # DOM identity is deliberately not part of the Worker action protocol.
            # Enhanced adapters may ground the visual point after the Worker decides
            # an action; vision-only execution uses the point unchanged.
            fixed_args.pop("target_ref", None)
            input_args.pop("target_ref", None)
            exposed_args = [name for name in exposed_args if name != "target_ref"]
        if capability == "drag":
            for name in ("to_x", "to_y"):
                fixed_args.pop(name, None)
                if name not in exposed_args:
                    exposed_args.append(name)
        if (
            capability == "select_option"
            and "text" not in fixed_args
            and "text" not in exposed_args
        ):
            exposed_args.append("text")
        if (
            capability == "open_url"
            and "url" not in fixed_args
            and "url" not in exposed_args
            and "url" not in input_args
        ):
            exposed_args.append("url")
        if (
            capability == "launch_app"
            and "app" not in fixed_args
            and "app" not in exposed_args
        ):
            exposed_args.append("app")
        normalized["fixed_args"] = fixed_args
        normalized["input_args"] = input_args
        normalized["exposed_args"] = exposed_args
        return normalized

    @model_validator(mode="after")
    def _no_duplicate_exposed_args(self) -> "DynamicActionSpec":
        if len(set(self.exposed_args)) != len(self.exposed_args):
            raise ValueError("exposed_args must be unique")
        spatial_bound = {"x", "y", "to_x", "to_y"}.intersection(self.input_args)
        if spatial_bound:
            raise ValueError(
                "spatial arguments are visual Worker decisions and cannot be runtime-bound: "
                f"{sorted(spatial_bound)}. To locate a record by a ResultRef value, bind that "
                "value to type.text for a visible search/filter action, then navigate visually."
            )
        bound = set(self.fixed_args).union(self.input_args)
        overlap = bound.intersection(self.exposed_args)
        if overlap:
            raise ValueError(
                f"arguments cannot be runtime-bound/fixed and exposed: {sorted(overlap)}"
            )
        duplicate_bound = set(self.fixed_args).intersection(self.input_args)
        if duplicate_bound:
            raise ValueError(
                f"arguments cannot be both fixed and runtime-bound: {sorted(duplicate_bound)}"
            )
        return self


class WorkerStrategy(StrictModel):
    """One replaceable, falsifiable execution approach for an immutable Worker goal."""

    approach: str = Field(min_length=1)


class WorkerSpec(StrictModel):
    """An immutable logical goal paired with one replaceable execution strategy."""

    profile: Literal["operator", "collector"] | None = Field(
        default=None,
        description=(
            "Execution result contract. When omitted, data requirements select collector "
            "and all other goals select operator."
        ),
    )
    goal: str
    success_criteria: list[str] = Field(min_length=1)
    input_refs: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Named ResultRefs routed by the reviewed Master. Runtime resolves them "
            "for bound action arguments without exposing collection data."
        ),
    )
    input_bindings: list[WorkerInputBinding] = Field(default_factory=list)
    data_requirements: list[DataRequirement] = Field(default_factory=list)
    strategy: WorkerStrategy

    @model_validator(mode="after")
    def _unique_ids(self) -> "WorkerSpec":
        if self.profile is None:
            self.profile = "collector" if self.data_requirements else "operator"
        requirement_ids = [item.id for item in self.data_requirements]
        if len(set(requirement_ids)) != len(requirement_ids):
            raise ValueError("data requirement ids must be unique")
        invalid_input_names = [
            name
            for name in self.input_refs
            if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) is None
        ]
        if invalid_input_names:
            raise ValueError(f"invalid input_refs names: {sorted(invalid_input_names)}")
        invalid_refs = [
            ref for ref in self.input_refs.values() if not ref.startswith("result:")
        ]
        if invalid_refs:
            raise ValueError("input_refs must contain ResultRef strings")
        binding_names = [item.name for item in self.input_bindings]
        if len(set(binding_names)) != len(binding_names):
            raise ValueError("input binding names must be unique")
        reserved = {
            name for name in binding_names
            if name in {
                "complete", "report_blocked", "continue_with_actions",
                *get_args(ToolActionCapability),
            }
            or name.startswith("runtime_")
        }
        if reserved:
            raise ValueError(
                f"input bindings use reserved Runtime action names: {sorted(reserved)}"
            )
        consumed_inputs = {item.input for item in self.input_bindings}
        unknown_inputs = consumed_inputs.difference(self.input_refs)
        if unknown_inputs:
            raise ValueError(
                f"input bindings reference unknown input_refs: {sorted(unknown_inputs)}"
            )
        unused_inputs = set(self.input_refs).difference(consumed_inputs)
        if unused_inputs:
            raise ValueError(
                "input_refs must be consumed by deterministic input_bindings: "
                f"{sorted(unused_inputs)}"
            )
        if self.profile == "collector" and len(self.data_requirements) != 1:
            raise ValueError("collector profile requires exactly one logical data requirement")
        if self.profile == "operator" and self.data_requirements:
            raise ValueError("operator profile cannot declare data requirements")
        return self


class WorkerState(StrictModel):
    """Compact evidence channel paired with a Worker action decision."""

    status: Literal["exploring", "collecting", "completed", "failed"]
    summary: str
    established_facts: list[str] = Field(
        default_factory=list,
        description=(
            "New exact visual observations not already present in WorkerMemory. Runtime "
            "keeps them only as bounded Worker narrative, never as authoritative completion "
            "evidence. Include only task evidence needed after leaving the current frame; "
            "exclude page chrome, dialogs, coordinates, approach alignment, and visible-control "
            "inventories. For later record matching, retain the complete application-declared "
            "identity without pronouns, prefixes, ellipses, summaries, or repetition."
        ),
    )


class DataChunkRef(StrictModel):
    kind: Literal["data_chunk"] = "data_chunk"
    ref: str
    requirement_id: str
    frame_id: str
    provider: Literal["vision", "structured"]
    row_count: int = Field(ge=0)
    row_schema: dict[str, Any]
    coverage: dict[str, Any] = Field(default_factory=dict)


class CollectionRef(StrictModel):
    kind: Literal["collection"] = "collection"
    ref: str
    requirement_id: str
    chunk_refs: list[str]
    row_count: int = Field(ge=0)
    row_schema: dict[str, Any]
    coverage: dict[str, Any] = Field(default_factory=dict)


class ResultRef(StrictModel):
    kind: Literal["result"] = "result"
    ref: str
    value_schema: dict[str, Any]
    summary: str = ""


class MaterializedFrame(StrictModel):
    frame_id: str
    screenshot_path: str
    readiness: Literal["ready", "loading", "blank"] = "ready"
    readiness_reason: str = ""
    platform_time: dict[str, Any] = Field(default_factory=dict)
    url: str = ""
    title: str = ""
    page_viewport: dict[str, Any] = Field(default_factory=dict)
    controls: list[dict[str, Any]] = Field(default_factory=list)
    visible_collection_regions: list[dict[str, Any]] = Field(default_factory=list)
    structured_surfaces: list[dict[str, Any]] = Field(default_factory=list)
    applied_filters: dict[str, Any] = Field(default_factory=dict)
    requirement_scopes: dict[str, dict[str, Any]] = Field(default_factory=dict)
    chunks: list[DataChunkRef] = Field(default_factory=list)
    collections: list[CollectionRef] = Field(default_factory=list)
    missing_requirements: list[str] = Field(default_factory=list)


class WorkerOutcome(StrictModel):
    phase: Literal["completed", "failed"]
    summary: str
    collection_ref: CollectionRef | None = None
    failure_kind: FailureKind | None = None
    steps: int


class ToolAgentRun(StrictModel):
    phase: Literal["completed", "failed"]
    summary: str
    effect: Literal["mutation", "data", "ui_state", "none"] = "none"
    output: Any = None
    result_ref: ResultRef | None = None
    trace: list[dict[str, Any]] = Field(default_factory=list)
    master_model: str
    worker_model: str
    perception_model: str
    perception_mode: Literal["vision-only", "enhanced"]
    platform_time: dict[str, Any] = Field(default_factory=dict)
