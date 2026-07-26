"""Neutral statement contracts used by the coding runtime and executors."""

from __future__ import annotations

from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, Field, JsonValue, model_validator

from gui_agent.core.schemas import (
    Coverage,
    InteractionIntent,
    OutputSpec,
    OutputType,
    PersistenceMode,
    StatementOutcome,
    Verification,
)


SurfaceName = Literal["main"]
CommandCapability = Literal["open_url", "back", "launch_app"]
ExecutorKind = Literal["interact", "acquire", "read", "command"]


class StatementNode(BaseModel):
    model_config = {"extra": "forbid"}

    id: str = ""
    bind: str | None = None
    returns: dict[str, OutputSpec] = Field(default_factory=dict)

    @property
    def goal_text(self) -> str:
        raise NotImplementedError


class Interact(StatementNode):
    op: Literal["interact"] = "interact"
    goal: str
    success: str
    interaction_intent: InteractionIntent = None
    on: SurfaceName = "main"
    required_values: dict[str, JsonValue] = Field(default_factory=dict)
    observe_fields: list[str] = Field(default_factory=list)
    scope: str = ""
    persistence: PersistenceMode = "immediate"

    @model_validator(mode="after")
    def _ui_postcondition_only(self) -> "Interact":
        if self.bind is not None or self.returns:
            raise ValueError("Interact cannot bind business outputs")
        return self

    @property
    def goal_text(self) -> str:
        return self.goal


class ObservationBinding(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}

    source: Literal["field", "page", "dataset"] = "field"
    name: str


class Read(StatementNode):
    op: Literal["read"] = "read"
    reads: dict[str, ObservationBinding] = Field(default_factory=dict)

    @property
    def goal_text(self) -> str:
        return "Bind observation: " + ", ".join(
            f"{output}<-{binding.source}.{binding.name}"
            for output, binding in self.reads.items()
        )


class Acquire(StatementNode):
    op: Literal["acquire"] = "acquire"
    goal: str
    on: SurfaceName = "main"
    required_fields: list[str] = Field(default_factory=list)

    @property
    def goal_text(self) -> str:
        return self.goal


class Command(StatementNode):
    op: Literal["command"] = "command"
    capability: CommandCapability
    on: SurfaceName = "main"
    args: dict[str, JsonValue] = Field(default_factory=dict)

    @property
    def goal_text(self) -> str:
        return self.capability


ExecutableStatement: TypeAlias = Interact | Acquire | Read | Command


class InputDescriptor(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}

    source_var: str
    producer: ExecutorKind
    output_name: str = ""
    type: OutputType | None = None
    coverage: Coverage = "current_view"
    verification: Verification = "confirmed"


class StatementInvocation(BaseModel):
    statement: ExecutableStatement = Field(discriminator="op")
    task_goal: str = ""
    inputs: dict[str, JsonValue] = Field(default_factory=dict)
    input_descriptors: dict[str, InputDescriptor] = Field(default_factory=dict)
    args: dict[str, JsonValue] = Field(default_factory=dict)
    loop_path: list[int] = Field(default_factory=list)

    @property
    def id(self) -> str:
        return self.statement.id

    @property
    def bind(self) -> str | None:
        return self.statement.bind

    @property
    def goal(self) -> str:
        return self.statement.goal_text

    @property
    def executor(self) -> ExecutorKind:
        return self.statement.op


class RunRecord(BaseModel):
    node_id: str = ""
    executor: ExecutorKind
    name: str
    var: str | None = None
    result: StatementOutcome
    loop_path: list[int] = Field(default_factory=list)
    instance_id: str = ""
    coding_op: str = ""
    coding_payload: dict[str, Any] = Field(default_factory=dict)
    coding_call_id: str = ""
    coding_plan: str = ""
    coding_plan_step: int = 0
    coding_plan_steps: int = 0


def matches_output_spec(value: JsonValue, spec: OutputSpec) -> bool:
    if value is None:
        return not spec.required
    if spec.type in {"text", "url"}:
        return isinstance(value, str) and (bool(value.strip()) or not spec.required)
    if spec.type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if spec.type == "boolean":
        return isinstance(value, bool)
    if spec.type == "record":
        return isinstance(value, dict) and all(field in value for field in spec.fields)
    if spec.type == "list[record]":
        return isinstance(value, list) and all(
            isinstance(row, dict) and all(field in row for field in spec.fields)
            for row in value
        )
    return True


__all__ = [
    "Acquire",
    "Command",
    "InputDescriptor",
    "Interact",
    "ObservationBinding",
    "OutputSpec",
    "Read",
    "RunRecord",
    "StatementInvocation",
    "matches_output_spec",
]
