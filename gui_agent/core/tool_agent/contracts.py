"""Typed contracts for the dynamic Master/Worker protocol."""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Literal, TypeAlias, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
        unknown_filters = set(self.filters).difference(properties)
        if unknown_filters:
            raise ValueError(
                "filter fields must be present in row_schema so logical row scope can "
                f"be verified: {sorted(unknown_filters)}"
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
    "back",
    "home",
    "app_switch",
    "launch_app",
    "ask_user",
]


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
    # "once": the ref is a scalar resolved once per Worker. "each": the ref is
    # an array consumed one element at a time; the Worker calls `complete` after
    # each element and the Runtime advances the shared cursor. Multiple `each`
    # bindings over the same ref stay aligned on one cursor (e.g. a plan row's
    # identity and target value).
    consume: Literal["once", "each"] = "once"


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
        if capability in {
            "tap", "type", "scroll", "drag", "long_press", "select_option",
        }:
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
    unresolved_inputs: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "User-owned values described by role but lacking an exact identifier. "
            "Each must be resolved before a dependent Commitment is established."
        ),
    )
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
            for name in (*self.input_refs, *self.unresolved_inputs)
            if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) is None
        ]
        if invalid_input_names:
            raise ValueError(f"invalid input names: {sorted(invalid_input_names)}")
        duplicate_inputs = set(self.input_refs).intersection(self.unresolved_inputs)
        if duplicate_inputs:
            raise ValueError(f"inputs cannot be both resolved and unresolved: {sorted(duplicate_inputs)}")
        empty_descriptions = [
            name for name, description in self.unresolved_inputs.items()
            if not description.strip()
        ]
        if empty_descriptions:
            raise ValueError(f"unresolved inputs need descriptions: {sorted(empty_descriptions)}")
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


class WorkerMemoryUpdate(StrictModel):
    """One Worker-owned source-fact delta emitted with a decision."""

    fact_type: Literal["observation", "evidence"]
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    status: Literal["active", "retracted"] = "active"
    lifetime: Literal["frame", "attempt"]
    statement: str = Field(min_length=1, max_length=1_200)
    depends_on: list[str] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def _validate_memory_semantics(self) -> "WorkerMemoryUpdate":
        if self.fact_type == "observation":
            if self.lifetime != "frame":
                raise ValueError("observations require lifetime='frame'")
            if self.status != "active":
                raise ValueError("frame observations expire automatically")
            if self.depends_on:
                raise ValueError("observations cannot have validity dependencies")
            return self
        if self.lifetime != "attempt":
            raise ValueError("evidence requires lifetime='attempt'")
        if self.status != "active":
            return self
        if self.depends_on:
            raise ValueError("evidence facts cannot have validity dependencies")
        return self


class WorkerState(StrictModel):
    """Compact state and typed memory deltas paired with one Worker decision."""

    status: Literal["exploring", "collecting", "executing", "completed", "failed"]
    summary: str
    memory_updates: list[WorkerMemoryUpdate] = Field(
        default_factory=list,
        max_length=8,
        description=(
            "Only Worker-owned source facts in this delta enter progress; summary is not memory. "
            "Current screenshot/MaterializedFrame is the Observation layer. Use "
            "observation/frame only for present-surface facts, which expire on the next "
            "frame; use evidence/attempt for verified facts that remain true after the frame "
            "changes. Evidence is "
            "never a prediction about what a visible control will do. A key is a stable fact "
            "slot, not an event or planned action; update or "
            "retract that key when the fact changes. Evidence records what was verified at "
            "its source frame and does not establish current visibility. Emit only new, "
            "corrected, or retracted versions; Runtime retains earlier active versions and "
            "alone owns Claims, Commitments, Receipts, and progress transitions."
        ),
    )

    @property
    def memory_statements(self) -> tuple[str, ...]:
        return tuple(
            item.statement for item in self.memory_updates
            if item.status == "active"
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
    visual_fingerprint: str = Field(
        default="",
        description=(
            "Runtime-private rendered-surface identity used for action loop detection; "
            "it is not projected into Worker context."
        ),
    )
    readiness: Literal["ready", "loading", "blank"] = "ready"
    readiness_reason: str = ""
    platform_time: dict[str, Any] = Field(default_factory=dict)
    url: str = ""
    title: str = ""
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
