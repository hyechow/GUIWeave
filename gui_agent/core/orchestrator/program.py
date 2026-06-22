"""DSL program AST for the orchestrator (MVP).

The orchestrator decomposes a user goal into a small DSL PROGRAM (not a DAG): a
sequence of milestone-level ``run()`` statements plus control flow (if / finish).
Each ``run()`` drives ONE linear GUI milestone via the linear executor and returns
a structured RunResult; the runner threads those results through variables and
conditions. This keeps the linear executor simple (one milestone, no logic) and
puts all branching/variables in the orchestrator — so "the middle read it but the
final output didn't know" disappears: every milestone's reads live in the env.

Grammar (MVP, no loops):
    var = run("<milestone>", returns=[...])      # returns = read_spec 字段
    var = run("<data query>", kind="data_query", sql="SELECT ...", returns=[...])
    if var["field"] == "value": <stmts> else: <stmts>
    finish("<message with {var[field]} refs>")
"""

from __future__ import annotations

import re
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, Field

# DSL data-flow template grammar: ``{var[field]}`` pulls a prior read's value out of the
# variable environment. Used by finish messages (the original site) AND — since the
# read-then-reference extension — by a run's name/success_condition/read_spec, so a later
# action authored as『编辑机器人 {r[实际名称]}』targets the concrete entity a read identified.
# Single source of truth: the runner fills these at execute time, the decomposer validates
# that every ref resolves to a real read field. Keep both ends on THIS regex.
TEMPLATE_RE = re.compile(r"\{(\w+)\[([^\]]+)\]\}")

# A bare {var} with no [field] — almost always a botched {var[field]} ref the LLM wrote forgetting
# our field syntax (e.g. {robot_name} instead of {robot_name[机器人名称]}). It neither resolves
# (TEMPLATE_RE needs the field) nor matches it, so the literal "{var}" leaks to the planner. The
# decomposer's validate flags it (when var is a read's var) so the repair pass fixes the form.
BARE_REF_RE = re.compile(r"\{(\w+)\}")

# The orchestrator's OWN linear-task vocabulary (decoupled from the executor's
# MilestoneKind). These are the milestone-sized things the linear executor is good
# at: 到某页 / 填一组表单 / 点一个按钮 / 读取一个结果 / 对结构化数据做只读查询。
# "read" and "data_query" are non-UI primitives consumed directly by the orchestrator.
RunKind = Literal["navigation", "filter", "action", "read", "data_query"]
CondCmp = Literal["==", "!=", "exists", "empty", "contains", "not_contains", "in", "not_in"]


class RunResult(BaseModel):
    """Return contract of one ``run()`` = one milestone driven to a terminal state.

    `reads` maps each requested `returns` field to the value the linear executor
    read off the result frame (读不到 = ""，按「当没有」处理，不让它卡住编排).
    `rows` is the LIST form: a read marked list_read returns one dict per row (the
    runtime-discovered collection a `foreach` iterates), and a `foreach` materializes
    its accumulated per-iteration rows here so a later data_query can query them."""

    completed: bool = False
    failed: bool = False
    reads: dict[str, str] = Field(default_factory=dict)
    rows: list[dict[str, str]] = Field(default_factory=list)
    summary: str = ""
    evidence: list[str] = Field(default_factory=list)


class Run(BaseModel):
    """Drive ONE linear milestone. `var` binds its RunResult; `returns` = fields to read."""

    op: Literal["run"] = "run"
    var: Optional[str] = None
    name: str
    success_condition: str = ""
    kind: RunKind = "action"
    returns: list[str] = Field(default_factory=list)
    # Task-level read instruction, authored by the decomposer from the user goal (not a
    # hardcoded prompt): for kind="read" it says what each `returns` field means and how to
    # judge it off the UI (which icon/colour/text carries it, what each value maps to). The
    # read primitive feeds this to structured_read as the primary judgment guidance; the app's
    # _check.md is a supplementary signal-convention reference. Empty for non-read runs.
    read_spec: str = Field(default="")
    # Restricted SQL for kind="data_query". It runs against the current structured table snapshot
    # in an in-memory sqlite database. Only SELECT / WITH ... SELECT is accepted.
    sql: str = Field(default="")
    # complete: reject partial table snapshots; current: explicitly query only currently
    # rendered rows. The default protects "entire history" / full-dataset tasks.
    data_scope: Literal["complete", "current"] = "complete"
    # STRUCTURAL marker for a precondition step ("确保已登录 / 已进入某模式"): a state to ENSURE,
    # not a fresh action. Set by the decomposer (an easy binary classification — far more reliable
    # than authoring a perfect gate). The engine rewrites a precondition's success_condition to a
    # generic "ensure-state" gate keyed on THIS flag (not on milestone-name keywords), so an
    # already-satisfied precondition (e.g. already logged in) is accepted on frame 1 instead of
    # stuck on a login-form / business-data gate. App-specific "what that state looks like" stays
    # in the checker's _check.md. The flag — not a string match — is the detection signal.
    precondition: bool = False
    # LIST read: extract one dict per matching row (RunResult.rows) instead of one scalar per field —
    # the runtime-discovered collection a `foreach` iterates (e.g. all visible review row ids). Only
    # meaningful on kind="read"; default False = the usual single-frame scalar read.
    list_read: bool = False


class Cond(BaseModel):
    """A single comparison against a prior run's read: ``var["field"] cmp value``."""

    var: str
    field: str
    cmp: CondCmp = "=="
    value: str = ""
    values: list[str] = Field(default_factory=list)


class If(BaseModel):
    op: Literal["if"] = "if"
    cond: Cond
    then: list["Stmt"] = Field(default_factory=list)
    otherwise: list["Stmt"] = Field(default_factory=list)


class Finish(BaseModel):
    """Produce the final reply. `message` is a template; ``{var[field]}`` placeholders
    are filled from the variable environment by the runner."""

    op: Literal["finish"] = "finish"
    message: str


class ForEach(BaseModel):
    """Iterate a runtime-discovered collection: run `body` once per row of a prior list_read's rows.

    The general iteration primitive (NOT a special "collect rows" kind): `over` names a kind="read"
    var whose RunResult.rows hold the collection (e.g. all review row ids). Each iteration binds
    `var` to that row, so `body` statements reference the current item with the usual {var[field]}
    template (e.g. 『打开 review {row[id]} 的详情』). Every read field produced inside the body is
    AUTO-accumulated, merged with the row's own fields, into one materialized row per iteration; the
    accumulated table is published to env under `into` so a following data_query can query the whole
    set (filter/aggregate). One level only — `body` may contain run/if/finish but not another foreach.
    """

    op: Literal["foreach"] = "foreach"
    var: str                                    # loop variable bound to each row, referenced as {var[field]}
    over: str                                   # the list_read Run's var whose .rows are iterated
    body: list["Stmt"] = Field(default_factory=list)
    into: str = ""                              # materialized-table var (defaults to f"{var}s" when empty)


Stmt = Annotated[Union[Run, If, Finish, ForEach], Field(discriminator="op")]


class Program(BaseModel):
    goal: str = ""
    statements: list[Stmt] = Field(default_factory=list)


If.model_rebuild()
ForEach.model_rebuild()
Program.model_rebuild()
