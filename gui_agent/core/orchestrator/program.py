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

from pydantic import BaseModel, Discriminator, Field, Tag

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

# The orchestrator's OWN command/query vocabulary (decoupled from the executor's
# MilestoneKind). These are the statement-sized things the script can express:
# navigate/filter/mutate through the GUI, or deterministically read/query structured data.
# Any UI command may declare returns/read_spec; those values are extracted from the command's
# completion frame. "read" remains as a compatibility/no-op current-frame primitive and for
# row-collection reads (legacy `over=` source for foreach). "data_query" is a non-UI primitive
# consumed directly by the orchestrator.
RunKind = Literal["navigation", "filter", "action", "read", "data_query"]
CondCmp = Literal["==", "!=", "exists", "empty", "contains", "not_contains", "in", "not_in"]

# Program-level execution modes. `navigation`/`filter`/`action` are commands: they may cross the
# GUI FFI boundary and can change page/state. `read`/`data_query`/`compute` are non-interactive
# statements: the interpreter executes them without clicking/filling/navigating. A browser URL/back
# navigation command may still be drained by a runtime non-UI fast path; its statement kind remains
# navigation.
INTERACTIVE_KINDS = frozenset({"navigation", "filter", "action"})
NON_INTERACTIVE_KINDS = frozenset({"read", "data_query", "compute"})


def execution_mode_for_kind(kind: str) -> Literal["interactive", "non_interactive"]:
    return "non_interactive" if kind in NON_INTERACTIVE_KINDS else "interactive"


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


class RunLike(BaseModel):
    """run 家族语句的共享形状（wire 上共用 op="run" + kind 区分）。

    脚本生成视角：程序 = 混合脚本；run 家族有两种执行模式——
    - 【交互命令】（Run：navigation/filter/action）= 对 GUI 执行器的函数调用，跨进
      非确定性世界的 FFI；合同（入口/后置/出参域/异常）全部压在这道边界上。
    - 【非交互语句】（Read/Query）= 解释器确定性执行的语句：消费当前帧/表格快照,
      不驱动 GUI，可安全重试、可被记忆化。
    分类轴是执行模式，不是数据方向——填表单和点链接同为交互执行；一个名义上的 read
    需要交互定位时会被【重新分类】为交互 Run（callframe 升格路径），不是在查询里
    "顺便"交互。三个节点在 IR 里平级（S8：字段各归其位），不再是子类关系。"""

    op: Literal["run"] = "run"
    var: Optional[str] = None
    name: str
    success_condition: str = ""
    kind: RunKind = "action"
    returns: list[str] = Field(default_factory=list)
    # Task-level return-read instruction, authored by the decomposer from the user goal (not a
    # hardcoded prompt): when `returns` is present it says what each field means and how to
    # judge it off the completion frame / query result. structured_read uses this as the primary
    # judgment guidance; app knowledge is a supplementary signal-convention reference.
    read_spec: str = Field(default="")

    @property
    def is_query(self) -> bool:
        """非交互语句（read / data_query）：不驱动 GUI——这正是它们能被解释器确定性执行的原因。"""
        return self.kind in {"read", "data_query"}

    @property
    def is_interactive(self) -> bool:
        """交互命令（navigation / filter / action）：驱动界面的 FFI。带 returns 的交互 run
        是「已发出 + 完成帧读值」的复合形态，依然是一次交互调用，不按读/写再细分。"""
        return self.kind in INTERACTIVE_KINDS

    @property
    def execution_mode(self) -> Literal["interactive", "non_interactive"]:
        return execution_mode_for_kind(self.kind)


class Run(RunLike):
    """【交互命令】：驱动一段连续的交互操作（一条 FROM→TO 边），由 GUI 执行器
    （milestone react loop——milestone 仅是执行器的内部载体格式）开到 done。
    `var` binds its RunResult; `returns` = fields to read from the completion frame."""

    # 交互命令的 kind 只剩交互词汇；read/data_query 是平级的 Read/Query 节点。
    kind: Literal["navigation", "filter", "action"] = "action"  # type: ignore[assignment]
    # Entry state of this interaction — the EXIT (success_condition) of the interaction that runs
    # just before it in the same linear block: FROM[i] := TO[i-1]. Derived deterministically by
    # `chain_from_states` (not authored by the LLM); empty at a block boundary (function/loop body
    # entry, where the entry is the call-site / loop-carry state). An interaction is an edge
    # FROM→TO; both endpoints are verifiable states (FROM via precondition entry-check, TO via the
    # checker), only the traversal between them is dynamic.
    from_state: str = ""
    # Typed returns: optional domain declaration per `returns` field —
    # {field: "url" | "number" | "date" | "enum:a|b|c" | "text"}. The callframe return-check
    # rejects a NON-empty value that falls outside its domain (读到垃圾 → 走空值同款有界恢复，
    # 而不是静默给错答)。Fields not listed fall back to conservative name-cue inference.
    return_domains: dict[str, str] = Field(default_factory=dict)
    # STRUCTURAL marker for a precondition step ("确保已登录 / 已进入某模式"): a state to ENSURE,
    # not a fresh action. Set by the decomposer (an easy binary classification — far more reliable
    # than authoring a perfect gate). The engine rewrites a precondition's success_condition to a
    # generic "ensure-state" gate keyed on THIS flag, so an already-satisfied precondition is
    # accepted on frame 1. App-specific "what that state looks like" stays in the checker's
    # _check.md. The flag — not a string match — is the detection signal.
    precondition: bool = False
    # STRUCTURAL declaration for set-realization: this single interactive step covers ALL members
    # of the named set entity via an aggregate mechanism the app provides (a parent record whose
    # save cascades to children, a select-all + mass action, a bulk-edit form). Value = the entity
    # mention it covers. The intent contract accepts a router-marked set WITHOUT a foreach when a
    # step declares coverage — knowledge decides WHEN aggregate coverage exists; this flag — not a
    # text pattern — is how the program states it.
    covers_set: str = ""


class Read(RunLike):
    """【非交互查询】frame read: extract `returns` off the CURRENT completion frame.
    解释器确定性执行——no UI action, page unchanged, FROM chain passes through."""

    kind: Literal["read"] = "read"  # type: ignore[assignment]


class Query(RunLike):
    """【非交互查询】data query: restricted SELECT over the current structured table
    snapshot (+ foreach-materialized tables). 解释器确定性执行——page unchanged."""

    kind: Literal["data_query"] = "data_query"  # type: ignore[assignment]
    # Restricted SQL. It runs against the current structured table snapshot in an in-memory
    # sqlite database. Only SELECT / WITH ... SELECT is accepted.
    sql: str = Field(default="")
    # complete: reject partial table snapshots; current: explicitly query only currently
    # rendered rows. The default protects "entire history" / full-dataset tasks.
    data_scope: Literal["complete", "current"] = "complete"


def _stmt_tag(value: object) -> str | None:
    """Callable discriminator: run-family ops share op="run" on the wire; route by kind.
    旧序列化（op=run + kind=read/data_query）自动落到平级的 Read/Query 节点。"""
    if isinstance(value, dict):
        op = str(value.get("op", "run") or "run")
        kind = str(value.get("kind", "action") or "action")
    else:
        op = str(getattr(value, "op", "run") or "run")
        kind = str(getattr(value, "kind", "action") or "action")
    if op != "run":
        return op
    return {"read": "read_stmt", "data_query": "query_stmt"}.get(kind, "run")


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
    # Legacy field kept for wire compatibility. For ordinary foreach/body=[] this is the row fields
    # to collect from the current list/grid. For agentic body_goal it historically meant the per-row
    # output contract. Prefer the explicit split below for new plans.
    returns: list[str] = Field(default_factory=list)
    # Explicit row-binding contract: fields collected from the current row before the body/body_goal
    # runs. body_goal templates may reference only these fields (or legacy template-derived fields).
    row_fields: list[str] = Field(default_factory=list)
    # Explicit materialized-table contract: fields the body/body_goal promises to publish per row
    # in addition to row_fields. data_query may only consume row_fields + output_fields (or legacy
    # returns for body_goal) from the foreach into table.
    output_fields: list[str] = Field(default_factory=list)
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


Stmt = Annotated[
    Union[
        Annotated[Run, Tag("run")],
        Annotated[Read, Tag("read_stmt")],
        Annotated[Query, Tag("query_stmt")],
        Annotated[If, Tag("if")],
        Annotated[Finish, Tag("finish")],
        Annotated[ForEach, Tag("foreach")],
        Annotated[Compute, Tag("compute")],
        Annotated[Call, Tag("call")],
    ],
    Discriminator(_stmt_tag),
]


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
