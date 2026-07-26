"""Semantic Program IR.

The compiler describes business intent, typed data flow, observation bindings
and explicit control flow. It deliberately does not describe page paths,
controls, SQL, Python expressions, or statement-internal branches.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias, Union

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from gui_agent.core.schemas import (
    InteractionIntent,
    OutputSpec,
    PersistenceMode,
)
from gui_agent.core.data_types import ComputeStep


SurfaceName = Literal["main"]
CommandCapability = Literal["open_url", "back", "launch_app"]
ConditionOperator = Literal[
    "==",
    "!=",
    "exists",
    "empty",
    "contains",
    "not_contains",
    "in",
    "not_in",
    ">",
    ">=",
    "<",
    "<=",
]


class ValueRef(BaseModel):
    """Reference a typed value already bound in Program scope."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    var: str
    path: list[str | int] = Field(default_factory=list)


class StatementNode(BaseModel):
    """Fields shared by executor-backed Program statements."""

    model_config = ConfigDict(extra="forbid")

    id: str = ""
    bind: str | None = None
    inputs: dict[str, ValueRef] = Field(default_factory=dict)
    returns: dict[str, OutputSpec] = Field(default_factory=dict)

    @property
    def goal_text(self) -> str:
        raise NotImplementedError


class Interact(StatementNode):
    """Reach one semantic UI postcondition on the current ``main`` surface."""

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
            raise ValueError(
                "Interact cannot bind business outputs; add an adjacent Read node"
            )
        return self

    @property
    def goal_text(self) -> str:
        return self.goal


class ObservationBinding(BaseModel):
    """Declared source of one Read output in the current observation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: Literal["field", "page", "dataset"] = "field"
    name: str


class Read(StatementNode):
    """Bind declared facts from the current observation."""

    op: Literal["read"] = "read"
    reads: dict[str, ObservationBinding] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _declare_observation_reads(self) -> "Read":
        if self.reads or not self.returns:
            return self
        outputs = list(self.returns)
        self.reads = {
            output: ObservationBinding(
                source=(
                    "dataset"
                    if self.returns[output].type == "list[record]"
                    else "page"
                    if output.casefold() in {"url", "title"}
                    else "field"
                ),
                name=("rows" if self.returns[output].type == "list[record]" else output),
            )
            for output in outputs
        }
        return self

    @property
    def goal_text(self) -> str:
        return "绑定当前 observation：" + "、".join(
            f"{output}<-{binding.source}.{binding.name}"
            for output, binding in self.reads.items()
        )


class SourceCheck(StatementNode):
    """Check whether declared semantic fields exist in one structural source."""

    op: Literal["source_check"] = "source_check"
    required_fields: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _fixed_outputs(self) -> "SourceCheck":
        expected = {
            "available": OutputSpec(type="boolean"),
            "bindings": OutputSpec(type="record"),
            "missing_fields": OutputSpec(type="json"),
        }
        if not self.returns:
            self.returns = expected
        return self

    @property
    def goal_text(self) -> str:
        return "检查 source fields：" + "、".join(self.required_fields)


class ComputeRef(BaseModel):
    """Reference a value produced by a deterministic Compute pipeline."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: list[str | int] = Field(default_factory=list)


class Compute(StatementNode):
    """Execute a Program-defined, side-effect-free data transformation."""

    op: Literal["compute"] = "compute"
    goal: str
    source: str
    required_fields: list[str] = Field(default_factory=list)
    steps: list[ComputeStep] = Field(min_length=1, max_length=10)
    outputs: dict[str, ComputeRef] = Field(min_length=1)

    @property
    def goal_text(self) -> str:
        return self.goal


class Acquire(StatementNode):
    """Materialize one already-scoped collection across reachable windows.

    Acquire never changes the business scope, exposes columns, opens records or
    performs data transforms.  Those choices belong to Interact / Compute / Program
    control flow respectively.
    """

    op: Literal["acquire"] = "acquire"
    goal: str
    on: SurfaceName = "main"
    source_check: ValueRef | None = None
    required_fields: list[str] = Field(default_factory=list)

    @property
    def goal_text(self) -> str:
        return self.goal


class Command(StatementNode):
    """Invoke one deterministic platform capability."""

    op: Literal["command"] = "command"
    capability: CommandCapability
    on: SurfaceName = "main"
    args: dict[str, JsonValue] = Field(default_factory=dict)
    arg_refs: dict[str, ValueRef] = Field(default_factory=dict)

    @property
    def goal_text(self) -> str:
        return self.capability


ExecutableStatement: TypeAlias = Interact | Acquire | Read | SourceCheck | Compute | Command


class Condition(BaseModel):
    """An explicit Program branch over one runtime value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ref: ValueRef
    cmp: ConditionOperator = "=="
    value: JsonValue = None
    values: list[JsonValue] = Field(default_factory=list)


class If(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["if"] = "if"
    cond: Condition
    then: list["Stmt"] = Field(default_factory=list)
    otherwise: list["Stmt"] = Field(default_factory=list)


class ForEach(BaseModel):
    """Deterministically run a fixed body over a materialized collection.

    ``items`` must resolve to a list.  Each iteration gets lexical ``item`` and
    optional ``index`` bindings.  When ``collect`` is set, that one value is
    appended to ``into`` after the body completes.  Membership selection,
    sorting and deduplication belong in a preceding Compute statement.
    """

    model_config = ConfigDict(extra="forbid")

    op: Literal["foreach"] = "foreach"
    items: ValueRef
    item: str = "item"
    index: str | None = None
    body: list["Stmt"] = Field(default_factory=list)
    collect: ValueRef | None = None
    into: str | None = None

    @model_validator(mode="after")
    def _validate_collection_contract(self) -> "ForEach":
        if bool(self.collect) != bool(self.into):
            raise ValueError("foreach collect and into must be declared together")
        if self.into and self.into in {self.item, self.index}:
            raise ValueError("foreach output cannot overwrite a lexical loop binding")
        return self


class Finish(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["finish"] = "finish"
    message: str = ""
    outputs: dict[str, ValueRef] = Field(default_factory=dict)


Stmt = Annotated[
    Union[Interact, Acquire, Read, SourceCheck, Compute, Command, If, ForEach, Finish],
    Field(discriminator="op"),
]


class Program(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = ""
    surface: SurfaceName = "main"
    statements: list[Stmt] = Field(default_factory=list)


def assign_statement_ids(program: Program) -> Program:
    """Assign stable source ids to executor-backed statements in DFS order."""

    counter = 0

    def visit(statements: list[Stmt]) -> None:
        nonlocal counter
        for statement in statements:
            if isinstance(statement, (Interact, Acquire, Read, SourceCheck, Compute, Command)):
                counter += 1
                if not statement.id:
                    statement.id = f"s{counter}"
            elif isinstance(statement, If):
                visit(statement.then)
                visit(statement.otherwise)
            elif isinstance(statement, ForEach):
                visit(statement.body)

    visit(program.statements)
    return program


If.model_rebuild()
ForEach.model_rebuild()
Program.model_rebuild()


__all__ = [
    "Acquire",
    "Command",
    "CommandCapability",
    "Compute",
    "ComputeRef",
    "Condition",
    "ExecutableStatement",
    "Finish",
    "ForEach",
    "If",
    "Interact",
    "ObservationBinding",
    "OutputSpec",
    "Program",
    "StatementNode",
    "Read",
    "SourceCheck",
    "Stmt",
    "ValueRef",
    "assign_statement_ids",
]
