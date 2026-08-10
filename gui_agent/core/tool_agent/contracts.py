"""Typed contracts for the dynamic Master/Worker protocol."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


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
    filters: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Required data-scope predicates keyed by normalized row field. The GUI "
            "Worker must establish this scope before collection begins."
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
                "filter fields must be present in row_schema so collection scope can "
                f"be verified: {sorted(unknown_filters)}"
            )
        unknown_sources = set(self.field_sources).difference(properties)
        if unknown_sources:
            raise ValueError(
                f"field_sources keys are absent from row_schema: {sorted(unknown_sources)}"
            )
        return self


class DynamicActionSpec(StrictModel):
    """One business-named action bound to a small runtime capability."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    capability: Literal["tap", "scroll", "select_option", "python_transform"]
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
        if not isinstance(data, dict) or data.get("capability") not in {
            "tap",
            "scroll",
            "select_option",
        }:
            return data
        normalized = dict(data)
        fixed_args = dict(normalized.get("fixed_args") or {})
        exposed_args = list(normalized.get("exposed_args") or [])
        for name in ("x", "y"):
            fixed_args.pop(name, None)
            if name not in exposed_args:
                exposed_args.append(name)
        if (
            normalized.get("capability") == "select_option"
            and "text" not in fixed_args
            and "text" not in exposed_args
        ):
            exposed_args.append("text")
        normalized["fixed_args"] = fixed_args
        normalized["exposed_args"] = exposed_args
        return normalized

    @model_validator(mode="before")
    @classmethod
    def _assign_runtime_ref_to_worker(cls, data: object) -> object:
        """Make runtime-created CollectionRefs a Worker-owned argument.

        A text-only Master cannot know which frame-bound collection the visual
        Worker will transform.  This mirrors the coordinate canonicalization
        above: the capability registry, rather than generated orchestration
        code, owns the required runtime argument.
        """
        if not isinstance(data, dict) or data.get("capability") != "python_transform":
            return data
        normalized = dict(data)
        fixed_args = dict(normalized.get("fixed_args") or {})
        exposed_args = list(normalized.get("exposed_args") or [])
        fixed_args.pop("data_ref", None)
        if "data_ref" not in exposed_args:
            exposed_args.append("data_ref")
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
    actions: list[DynamicActionSpec] = Field(min_length=1)
    result_schema: dict[str, Any]
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
        return self


class WorkerState(StrictModel):
    """Visible state-machine channel emitted in assistant ``content``."""

    status: Literal["exploring", "collecting", "computing", "completed", "failed"]
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
    result_ref: ResultRef | None = None
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
