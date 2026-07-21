"""Semantic Program IR.

The compiler describes business intent, typed data flow and explicit control
flow.  It deliberately does not describe page paths, controls, SQL, Python
expressions, or statement-internal branches.  Runtime executors decide those
details against the real UI or data context.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias, Union

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from gui_agent.core.schemas import OutputSpec, PersistenceMode
from gui_agent.core.data_types import DataStep


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
    on: SurfaceName = "main"
    required_values: dict[str, JsonValue] = Field(default_factory=dict)
    scope: str = ""
    persistence: PersistenceMode = "immediate"

    @model_validator(mode="after")
    def _ui_postcondition_only(self) -> "Interact":
        if self.bind is not None or self.returns:
            raise ValueError(
                "Interact cannot bind business outputs; add an adjacent Data statement"
            )
        return self

    @property
    def goal_text(self) -> str:
        return self.goal


class Data(StatementNode):
    """Read the current observation or bind its semantic source fields."""

    op: Literal["data"] = "data"
    goal: str
    mode: Literal["read", "inspect"] = "read"
    required_fields: list[str] = Field(default_factory=list)

    @property
    def goal_text(self) -> str:
        return self.goal


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
    steps: list[DataStep] = Field(min_length=1, max_length=10)
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


ExecutableStatement: TypeAlias = Interact | Acquire | Data | Compute | Command


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
    Union[Interact, Acquire, Data, Compute, Command, If, ForEach, Finish],
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
            if isinstance(statement, (Interact, Acquire, Data, Compute, Command)):
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
    "Data",
    "ExecutableStatement",
    "Finish",
    "ForEach",
    "If",
    "Interact",
    "OutputSpec",
    "Program",
    "StatementNode",
    "Stmt",
    "ValueRef",
    "assign_statement_ids",
]
