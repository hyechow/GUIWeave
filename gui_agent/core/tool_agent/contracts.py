"""Typed contracts for the dynamic Master/Worker protocol."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


DataFieldType = Literal["text", "number", "money", "datetime", "boolean"]


class DataRequirement(StrictModel):
    """A semantic, provider-independent request for a logical collection."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    description: str
    target_label: str = Field(
        default="",
        description="Visible label/caption that identifies the data surface, when known.",
    )
    scope: Literal["collection"] = "collection"
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
    coverage: Literal["complete"] = "complete"

    @model_validator(mode="before")
    @classmethod
    def _expand_row_schema_shorthand(cls, data: object) -> object:
        """Repair the common ``{field: type}`` shorthand into real JSON Schema."""
        if not isinstance(data, dict):
            return data
        schema = data.get("row_schema")
        if (
            isinstance(schema, dict)
            and "type" not in schema
            and "properties" not in schema
            and schema
            and all(value in {"string", "number", "integer", "boolean"} for value in schema.values())
        ):
            data = dict(data)
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
            if json_type not in expected_json_types[field_type]:
                raise ValueError(
                    f"field_types[{field!r}]={field_type!r} is incompatible with "
                    f"row_schema type {json_type!r}"
                )
            if field_type == "datetime":
                field_schema.setdefault("format", "date-time")
        self.row_schema = schema
        return self


ToolActionCapability: TypeAlias = Literal[
    "tap",
    "type",
    "clear_text",
    "press_enter",
    "scroll",
    "select_option",
    "open_url",
    "back",
]


class DynamicActionSpec(StrictModel):
    """One business-named action bound to a small runtime capability."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    capability: ToolActionCapability
    description: str
    fixed_args: dict[str, Any] = Field(default_factory=dict)
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
        exposed_args = list(normalized.get("exposed_args") or [])
        capability = normalized.get("capability")
        if capability in {"tap", "type", "scroll", "select_option"}:
            for name in ("x", "y"):
                fixed_args.pop(name, None)
                if name not in exposed_args:
                    exposed_args.append(name)
            # DOM identity is deliberately not part of the Worker action protocol.
            # Enhanced adapters may ground the visual point after the Worker decides
            # an action; vision-only execution uses the point unchanged.
            fixed_args.pop("target_ref", None)
            exposed_args = [name for name in exposed_args if name != "target_ref"]
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
        normalized["fixed_args"] = fixed_args
        normalized["exposed_args"] = exposed_args
        return normalized

    @model_validator(mode="after")
    def _no_duplicate_exposed_args(self) -> "DynamicActionSpec":
        if len(set(self.exposed_args)) != len(self.exposed_args):
            raise ValueError("exposed_args must be unique")
        overlap = set(self.fixed_args).intersection(self.exposed_args)
        if overlap:
            raise ValueError(f"arguments cannot be fixed and exposed: {sorted(overlap)}")
        return self


class WorkerSpec(StrictModel):
    """A dynamic agentic execution unit created by the Master."""

    profile: Literal["operator", "collector"] | None = Field(
        default=None,
        description=(
            "General GUI decision strategy. When omitted, data requirements select "
            "collector and all other goals select operator."
        ),
    )
    goal: str
    success_criteria: list[str] = Field(min_length=1)
    data_requirements: list[DataRequirement] = Field(default_factory=list)
    acquisition_filters: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Mutable UI scope used by this physical Worker attempt. Collector "
            "attempts default to the immutable logical DataRequirement.filters."
        ),
    )
    actions: list[DynamicActionSpec] = Field(min_length=1)
    max_steps: int = Field(default=8, ge=1, le=20)

    @model_validator(mode="after")
    def _unique_ids(self) -> "WorkerSpec":
        if self.profile is None:
            self.profile = "collector" if self.data_requirements else "operator"
        action_names = [item.name for item in self.actions]
        requirement_ids = [item.id for item in self.data_requirements]
        if len(set(action_names)) != len(action_names):
            raise ValueError("worker action names must be unique")
        if len(set(requirement_ids)) != len(requirement_ids):
            raise ValueError("data requirement ids must be unique")
        if self.profile == "collector" and len(self.data_requirements) != 1:
            raise ValueError("collector profile requires exactly one logical data requirement")
        if self.profile == "operator" and self.data_requirements:
            raise ValueError("operator profile cannot declare data requirements")
        if self.profile == "operator":
            if self.acquisition_filters:
                raise ValueError("operator profile cannot declare acquisition_filters")
            self.acquisition_filters = {}
        else:
            requirement = self.data_requirements[0]
            if self.acquisition_filters is None:
                self.acquisition_filters = deepcopy(requirement.filters)
            properties = set((requirement.row_schema.get("properties") or {}).keys())
            unknown_filters = set(self.acquisition_filters).difference(properties)
            if unknown_filters:
                raise ValueError(
                    "acquisition filter fields must be present in row_schema: "
                    f"{sorted(unknown_filters)}"
                )
        return self


class WorkerState(StrictModel):
    """Visible state-machine channel emitted in assistant ``content``."""

    status: Literal["exploring", "collecting", "completed", "failed"]
    summary: str
    established_facts: list[str] = Field(default_factory=list)
    open_gaps: list[str] = Field(default_factory=list)
    coverage: dict[str, str] = Field(default_factory=dict)
    action_space_status: Literal["sufficient", "missing_action"] = "sufficient"
    missing_action: str = ""
    next_instruction: str


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
    url: str = ""
    title: str = ""
    controls: list[dict[str, Any]] = Field(default_factory=list)
    applied_filters: dict[str, Any] = Field(default_factory=dict)
    requirement_scopes: dict[str, dict[str, Any]] = Field(default_factory=dict)
    chunks: list[DataChunkRef] = Field(default_factory=list)
    collections: list[CollectionRef] = Field(default_factory=list)
    missing_requirements: list[str] = Field(default_factory=list)


class WorkerOutcome(StrictModel):
    phase: Literal["completed", "failed"]
    summary: str
    collection_ref: CollectionRef | None = None
    steps: int


class ToolAgentRun(StrictModel):
    phase: Literal["completed", "failed"]
    summary: str
    output: Any = None
    result_ref: ResultRef | None = None
    trace: list[dict[str, Any]] = Field(default_factory=list)
    master_model: str
    worker_model: str
    perception_model: str
    perception_mode: Literal["vision-only", "enhanced"]
