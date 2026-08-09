"""Typed contracts for the dynamic Master/Worker protocol."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DataRequirement(StrictModel):
    """A semantic, provider-independent request for data in observed frames."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    description: str
    target_label: str = Field(
        default="",
        description="Visible label/caption that identifies the data surface, when known.",
    )
    scope: Literal["current_view"] = "current_view"
    row_schema: dict[str, Any]
    field_sources: dict[str, str] = Field(
        default_factory=dict,
        description="Normalized output field to platform-visible source header.",
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


class DynamicActionSpec(StrictModel):
    """One business-named action bound to a small runtime capability."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    capability: Literal["tap", "scroll", "python_transform"]
    description: str
    fixed_args: dict[str, Any] = Field(default_factory=dict)
    exposed_args: list[str] = Field(default_factory=list)

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

    goal: str
    success_criteria: list[str] = Field(min_length=1)
    data_requirements: list[DataRequirement] = Field(default_factory=list)
    actions: list[DynamicActionSpec] = Field(min_length=1)
    result_schema: dict[str, Any]
    max_steps: int = Field(default=8, ge=1, le=20)

    @model_validator(mode="after")
    def _unique_ids(self) -> "WorkerSpec":
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
