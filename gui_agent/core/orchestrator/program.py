"""DSL program AST for the orchestrator (MVP).

The orchestrator decomposes a user goal into a small DSL PROGRAM (not a DAG): a
sequence of milestone-level ``run()`` statements plus control flow (if / finish).
Each ``run()`` drives ONE linear GUI milestone via the linear executor and returns
a structured RunResult; the runner threads those results through variables and
conditions. This keeps the linear executor simple (one milestone, no logic) and
puts all branching/variables in the orchestrator — so "the middle read it but the
final output didn't know" disappears: every milestone's reads live in the env.

Grammar (MVP, no loops):
    var = run("<milestone>", returns=[...])      # returns = fields read from the completion frame
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
# result-then-reference extension — by a run's name/success_condition/read_spec, so a later
# action authored as『编辑机器人 {r[实际名称]}』targets the concrete entity a prior result identified.
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
# at: 到某页 / 填一组表单 / 点一个按钮 / 对结构化数据做只读查询。
# Any UI run may declare returns/read_spec; those values are extracted from the run's
# completion frame. "read" remains as a compatibility/no-op current-frame primitive and for
# row-collection reads (legacy `over=` source for foreach). "data_query" is a non-UI primitive
# consumed directly by the orchestrator.
RunKind = Literal["navigation", "filter", "action", "read", "data_query"]
CondCmp = Literal["==", "!=", "exists", "empty", "contains", "not_contains", "in", "not_in"]


class RunResult(BaseModel):
    """Return contract of one ``run()`` = one milestone driven to a terminal state.

    `reads` maps each requested `returns` field to the value the linear executor
    read off the result frame (读不到 = ""，按「当没有」处理，不让它卡住编排).
    `rows` is the LIST form: a row-collection read returns one dict per row (the
    runtime-discovered collection a `foreach` iterates), and a `foreach` materializes
    its accumulated per-iteration rows here so a later data_query can query them."""

    completed: bool = False
    failed: bool = False
    reads: dict[str, str] = Field(default_factory=dict)
    rows: list[dict[str, str]] = Field(default_factory=list)
    summary: str = ""
    evidence: list[str] = Field(default_factory=list)


class Run(BaseModel):
    """Drive ONE linear milestone. `var` binds its RunResult; `returns` = fields to read from completion."""

    op: Literal["run"] = "run"
    var: Optional[str] = None
    name: str
    success_condition: str = ""
    # Entry state of this milestone — the EXIT (success_condition) of the milestone that runs just
    # before it in the same linear block: FROM[i] := TO[i-1]. Derived deterministically by
    # `chain_from_states` (not authored by the LLM); empty at a block boundary (function/loop body
    # entry, where the entry is the call-site / loop-carry state). A milestone is an edge FROM→TO;
    # both endpoints are verifiable states (FROM via precondition entry-check, TO via the checker),
    # only the traversal between them is dynamic. Used to ground instruction phrasing / continuity.
    from_state: str = ""
    kind: RunKind = "action"
    returns: list[str] = Field(default_factory=list)
    # Task-level return-read instruction, authored by the decomposer from the user goal (not a
    # hardcoded prompt): when `returns` is present it says what each field means and how to
    # judge it off the UI completion frame (which icon/colour/text carries it, what each value
    # maps to). structured_read uses this as the primary judgment guidance; app knowledge is a
    # supplementary signal-convention reference.
    read_spec: str = Field(default="")
    # Typed returns: optional domain declaration per `returns` field —
    # {field: "url" | "number" | "date" | "enum:a|b|c" | "text"}. The callframe return-check
    # rejects a NON-empty value that falls outside its domain (读到垃圾 → 走空值同款有界恢复，
    # 而不是静默给错答)。Fields not listed fall back to conservative name-cue inference.
    return_domains: dict[str, str] = Field(default_factory=dict)
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
    """Iterate a runtime-discovered collection: run `body` once per row of a prior row-collection read.

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
    over: str = ""                              # the row-collection read's var whose .rows are iterated (legacy path); empty = use collect_fn/target
    target: str = ""                            # browser path: collect target description (what table/collection to fetch)
    returns: list[str] = Field(default_factory=list)  # browser path: fields to collect per row
    body: list["Stmt"] = Field(default_factory=list)
    # Per-row SUB-GOAL (agentic body): when set, the body is NOT pre-baked Stmts — instead, for
    # each row, the sub-goal text (templated with `{var[field]}`) is decomposed fresh at runtime
    # into a sub-program and driven by the full agent loop (plan/replan/checker), and the
    # sub-program's declared `returns` are merged back into the row. Use for per-row tasks too
    # complex for a fixed step list — e.g. "for this child variant, find its parent configurable
    # product and read the primary Material" (derive key → search → disambiguate → open → read).
    # Mutually exclusive with `body`. One level only: the sub-program may not itself use body_goal.
    body_goal: str = ""
    # Progressive orchestration, selection-only form: a SEMANTIC description of which collected rows
    # belong to the target set (e.g. 「size 28 的 Sahara leggings 变体」). Set together with an
    # explicit `body`: the checkpoint makes ONE selection call against the REAL rows (membership as
    # data — cross-family 16/16 offline) and runs the t=0-authored body on the selected rows only.
    # Body authoring stays at t=0 (mature decomposer prompt, full validator gates, offline-verifiable);
    # ONLY the decision that genuinely needs runtime data is deferred. Empty ⇒ iterate all rows.
    member_desc: str = ""
    into: str = ""                              # materialized-table var (defaults to f"{var}s" when empty)
    limit: int | None = None                    # stop after collecting this many rows (None = collect all); use for sorted top-K grids


class Compute(BaseModel):
    """A PURE-COMPUTE statement — deterministic value derivation the interpreter evaluates itself,
    NOT a GUI milestone. Separates compute (CPU) from GUI effect (agent): e.g. deriving a parent
    SKU/base name by stripping a variant suffix is a string op, NOT something the agent should do
    by vision-while-operating (that overloaded the milestone and made the agent stall). `expr` is a
    restricted Python expression over scalar variables in scope (params + prior compute results),
    referenced by bare name; result binds to scalar `var`. Whitelisted ops only (str methods, slice,
    re.sub/search) — no calls, attributes, or names outside the whitelist."""

    op: Literal["compute"] = "compute"
    var: str                                    # scalar variable the result binds to (referenced as {var})
    expr: str                                   # restricted expression, e.g. name.rsplit('-', 2)[0]


class Call(BaseModel):
    """Invoke a FunctionDef. `args` maps each param name → a value template (resolved in the CALLER's
    scope: {row[field]} from a loop row, {var} from a scalar, or a literal). The callee runs in a
    fresh scalar frame with those params bound; its declared `returns` are collected and bound to
    `var` as a RunResult (referenced downstream as {var[field]}). Callable anywhere — main, an if
    branch, or a foreach body — functions are NOT loop-bound."""

    op: Literal["call"] = "call"
    func: str                                   # FunctionDef name to invoke
    args: dict[str, str] = Field(default_factory=dict)  # param name → value template (caller scope)
    var: Optional[str] = None                   # bind the function's returns here (a RunResult)


Stmt = Annotated[Union[Run, If, Finish, ForEach, Compute, Call], Field(discriminator="op")]


class FunctionDef(BaseModel):
    """A reusable, parameterized sub-program — a function in the DSL, decoupled from any loop. Its
    `body` is a normal statement list (milestones / compute / if / nested calls); `params` are bound
    as scalars on entry; `returns` names the scalar/read fields exposed to the caller on exit. The
    whole program (main + all functions) is produced in ONE decompose — like writing a code file —
    and each function is decomposed ONCE and called N times (no per-row re-decompose)."""

    name: str
    params: list[str] = Field(default_factory=list)
    body: list[Stmt] = Field(default_factory=list)
    returns: list[str] = Field(default_factory=list)


class Program(BaseModel):
    goal: str = ""
    statements: list[Stmt] = Field(default_factory=list)
    functions: list[FunctionDef] = Field(default_factory=list)


If.model_rebuild()
ForEach.model_rebuild()
FunctionDef.model_rebuild()
Program.model_rebuild()
