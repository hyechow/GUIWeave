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
# at: 到某页 / 填一组表单 / 点一个按钮 / 读取一个结果。 "read" = read-only single-frame
# result milestone (the inspect concept). A thin adapter maps these to whatever the
# real per-milestone driver expects when it's wired in.
RunKind = Literal["navigation", "filter", "action", "read"]


class RunResult(BaseModel):
    """Return contract of one ``run()`` = one milestone driven to a terminal state.

    `reads` maps each requested `returns` field to the value the linear executor
    read off the result frame (读不到 = ""，按「当没有」处理，不让它卡住编排)."""

    completed: bool = False
    failed: bool = False
    reads: dict[str, str] = Field(default_factory=dict)
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
    # STRUCTURAL marker for a precondition step ("确保已登录 / 已进入某模式"): a state to ENSURE,
    # not a fresh action. Set by the decomposer (an easy binary classification — far more reliable
    # than authoring a perfect gate). The engine rewrites a precondition's success_condition to a
    # generic "ensure-state" gate keyed on THIS flag (not on milestone-name keywords), so an
    # already-satisfied precondition (e.g. already logged in) is accepted on frame 1 instead of
    # stuck on a login-form / business-data gate. App-specific "what that state looks like" stays
    # in the checker's _check.md. The flag — not a string match — is the detection signal.
    precondition: bool = False


class Cond(BaseModel):
    """A single comparison against a prior run's read: ``var["field"] cmp value``."""

    var: str
    field: str
    cmp: Literal["==", "!="] = "=="
    value: str


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


Stmt = Annotated[Union[Run, If, Finish], Field(discriminator="op")]


class Program(BaseModel):
    goal: str = ""
    statements: list[Stmt] = Field(default_factory=list)


If.model_rebuild()
Program.model_rebuild()
