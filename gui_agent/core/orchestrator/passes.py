"""Compilation passes: AST-level normalizations over a decomposed DSL Program.

The orchestrator is a compiler frontend (decomposer) → these deterministic passes (compiler
middle-end) → validator/preflight (type-check/lint). Each pass takes a Program and returns a NEW
Program (inputs untouched); all are idempotent. Run order lives in decomposer.to_program:
collapse_foreach_enrichment_passes → insert_loop_entry_arrivals → normalize_confirm_read_gates /
normalize_precondition_gates → chain_from_states. Marshalling into the executor's Milestone format
is NOT here — interactive execution belongs to core/run/interactive.py.
"""

from __future__ import annotations

import re
from typing import Optional

from .program import (
    INTERACTIVE_KINDS,
    TEMPLATE_RE,
    Call,
    Compute,
    Finish,
    ForEach,
    If,
    Program,
    Query,
    Read,
    Run,
    RunLike,
    Stmt,
)


# ── action return-read structural backstop (L2) ──────────────────────────────────────
# Older plans may emit an interactive Run followed by a scalar Read. Merge the read contract into
# the Run so extraction happens from its completion frame. The authored success condition remains
# unchanged: interaction completion and result extraction are independent contracts.
def _normalize_stmts(stmts: list[Stmt]) -> list[Stmt]:
    out: list[Stmt] = []
    i = 0
    while i < len(stmts):
        s = stmts[i]
        nxt = stmts[i + 1] if i + 1 < len(stmts) else None
        if (
            isinstance(s, Run)
            and isinstance(nxt, Read)
            and s.kind in INTERACTIVE_KINDS
            and nxt.returns
            and (not s.var or not nxt.var or s.var == nxt.var)
        ):
            update = {
                "var": nxt.var or s.var,
                "returns": list(nxt.returns),
                "read_spec": nxt.read_spec,
            }
            out.append(s.model_copy(update=update))
            i += 2
            continue
        if isinstance(s, If):
            out.append(s.model_copy(update={
                "then": _normalize_stmts(s.then),
                "otherwise": _normalize_stmts(s.otherwise),
            }))
        else:
            out.append(s)
        i += 1
    return out


def normalize_confirm_read_gates(program: Program) -> Program:
    """Normalize legacy action->read pairs into action return contracts.

    Older plans express result extraction as a scalar read Run immediately after a UI Run.
    Newer plans put ``returns``/``read_spec`` on that UI Run directly. This pass makes both
    shapes execute the same way by merging compatible scalar read pairs into the UI Run. The
    interaction success condition is never rewritten. A following data_query remains separate.
    Recurses into if-branches and returns a new Program; the pass is idempotent."""
    return program.model_copy(update={"statements": _normalize_stmts(program.statements)})


# ── precondition gate backstop (L2) ──────────────────────────────────────────────────
# A precondition step ("确保已登录 / 已进入某模式") ENSURES a state; it must accept on the
# data-independent target state — NOT on a mid-progress interface that only exists while the
# precondition is UNMET. Two real stuck regressions, both login: 20260615_153314 gated on the
# login FORM (an already-logged-in session can't return to it → never met), 162312 gated on
# business-data content (cards/data empty until LATER steps produce them → circular). The
# decomposer's rule 9 *asks* for a clean gate but is unreliable (~1/8). This pass *guarantees*
# it — keyed on the STRUCTURAL `run.precondition` flag the decomposer sets (NOT on milestone-name
# keywords like 登录/认证, which mis-fire on 认证设备/登录日志查询 and miss other phrasings/langs;
# the flag is to this pass what action→read adjacency is to confirm-read). The gate is app-AGNOSTIC;
# the app-specific "what that state looks like" lives in the checker's _check.md, which JUDGES this
# gate. That split is load-bearing and verified: a generic gate + _check.md → done, but a form gate
# + _check.md → still stuck, because the milestone's success_condition binds and _check.md
# (authoritative only over GENERAL checker rules) can't override it — so the gate is fixed here.

_PRECONDITION_GATE_TMPL = (
    "已处于该前置步要求的目标状态（如已登录、已进入某模式/某页）："
    "显示对应的稳定标志（功能区/导航就位，与业务数据无关），"
    "而非只在未完成态才出现的中间界面（如登录表单）。"
)
# NOTE: do not name `_check.md` (or any internal knowledge-overlay filename) in this
# template — it is injected into the checker/supervisor prompt via milestone.success_condition,
# so a literal filename leaks an internal concept into the LLM context for zero benefit (the LLM
# can't open the file). The app-specific "what done looks like" content is delivered through the
# separate checker-only channel (supervisor._check_knowledge), independent of this string.


def _normalize_precondition_stmts(stmts: list[Stmt]) -> list[Stmt]:
    out: list[Stmt] = []
    for s in stmts:
        if isinstance(s, Run) and s.precondition:
            out.append(s.model_copy(update={"success_condition": _PRECONDITION_GATE_TMPL}))
        elif isinstance(s, If):
            out.append(s.model_copy(update={
                "then": _normalize_precondition_stmts(s.then),
                "otherwise": _normalize_precondition_stmts(s.otherwise),
            }))
        else:
            out.append(s)
    return out


def normalize_precondition_gates(program: Program) -> Program:
    """Rewrite every precondition step's success_condition to the generic ensure-state gate.

    Detection is the STRUCTURAL `run.precondition` flag (set by the decomposer), not name keywords —
    so it's robust to phrasing/language and covers any precondition (login / enter-mode / open-page),
    not just login. Recurses into if-branches; returns a NEW Program (inputs untouched); idempotent.
    The checker may recognize this state, but runtime completion still requires evidence that the
    navigation edge executed; app-specific markers stay in the checker's _check.md."""
    return program.model_copy(update={"statements": _normalize_precondition_stmts(program.statements)})


def finalize_gates(program: Program) -> Program:
    """Post-validation finalize: the two gate normalizations in the historical caller order
    normalize_precondition_gates(normalize_confirm_read_gates(...)). These run AFTER validate (they
    rewrite kind/success_condition and would defeat kind-keyed validator rules; see to_program's
    docstring), so they belong here — applied ONCE by decompose/redecompose so every generation
    entrance (AOT decompose / JIT subdecompose / kickback redecompose) gets them centrally, instead
    of each of the 7 call sites re-wrapping the output by hand (S9b). Idempotent."""
    return normalize_precondition_gates(normalize_confirm_read_gates(program))


# ── foreach compiler normalization ────────────────────────────────────────────────
# A common malformed decomposition for per-row enrichment is:
#   foreach row -> products_rows returns=[sku, action_url], body=[]
#   foreach row -> enriched returns=[material], body=[call resolve({row[sku]}, {row[action_url]})]
#
# In the browser path, an over="" foreach means "collect rows from the CURRENT grid". The second
# loop above would therefore re-collect the grid using `returns=[material]`, then try to call the
# function with row[sku]/row[action_url] fields that are not in that loop's row contract. The
# compiler can repair this deterministically: it is really one loop that collects the first loop's
# row capabilities and runs the second loop's enrichment body, materializing into the second loop's
# target table. This is site-agnostic typed data-flow normalization, not task knowledge.

def _refs_to_loop_var(stmts: list[Stmt], loop_var: str) -> set[str]:
    refs: set[str] = set()
    for s in stmts:
        if isinstance(s, RunLike):
            for text in (s.name, s.success_condition, s.read_spec):
                refs.update(field.strip().strip("'\"") for var, field in TEMPLATE_RE.findall(text or "") if var == loop_var)
        elif isinstance(s, Call):
            for value in (s.args or {}).values():
                refs.update(field.strip().strip("'\"") for var, field in TEMPLATE_RE.findall(str(value)) if var == loop_var)
        elif isinstance(s, Finish):
            refs.update(field.strip().strip("'\"") for var, field in TEMPLATE_RE.findall(s.message or "") if var == loop_var)
        elif isinstance(s, If):
            if s.cond.var == loop_var:
                refs.add(s.cond.field)
            refs.update(_refs_to_loop_var(s.then, loop_var))
            refs.update(_refs_to_loop_var(s.otherwise, loop_var))
        elif isinstance(s, ForEach):
            refs.update(_refs_to_loop_var(s.body, loop_var))
    return refs


def _lower_set(values: list[str] | set[str]) -> set[str]:
    return {str(v).strip().lower() for v in values if str(v).strip()}


def _can_collapse_foreach_pair(first: ForEach, second: ForEach) -> bool:
    first_fields = first.row_fields or first.returns
    if first.body or first.body_goal or not first_fields:
        return False
    if not second.body or second.body_goal:
        return False
    first_table = first.into or f"{first.var}s"
    if second.over and second.over != first_table:
        return False
    refs = _refs_to_loop_var(second.body, second.var)
    if not refs:
        return False
    return _lower_set(refs).issubset(_lower_set(first_fields))


def _collapse_foreach_stmts(stmts: list[Stmt]) -> list[Stmt]:
    out: list[Stmt] = []
    i = 0
    while i < len(stmts):
        s = stmts[i]
        nxt = stmts[i + 1] if i + 1 < len(stmts) else None
        if isinstance(s, ForEach) and isinstance(nxt, ForEach) and _can_collapse_foreach_pair(s, nxt):
            row_fields = list(s.row_fields or s.returns)
            out.append(nxt.model_copy(update={
                "over": "",
                "target": s.target or nxt.target,
                "returns": list(s.returns),
                "row_fields": row_fields,
                "limit": s.limit if s.limit is not None else nxt.limit,
            }))
            i += 2
            continue
        if isinstance(s, If):
            out.append(s.model_copy(update={
                "then": _collapse_foreach_stmts(s.then),
                "otherwise": _collapse_foreach_stmts(s.otherwise),
            }))
        elif isinstance(s, ForEach):
            out.append(s.model_copy(update={"body": _collapse_foreach_stmts(s.body)}))
        else:
            out.append(s)
        i += 1
    return out


def collapse_foreach_enrichment_passes(program: Program) -> Program:
    """Fold row-collection foreach + per-row enrichment foreach into one typed loop.

    Returns a NEW Program (inputs untouched); idempotent. Function bodies are left unchanged because
    this normalization is about materialized table loops in statement blocks, not reusable helper
    bodies.
    """

    return program.model_copy(update={"statements": _collapse_foreach_stmts(program.statements)})


# A loop/function body that STARTS by acting on a list page (filter/action) and then drills INTO a
# record (a later navigation) leaves the page on that record's detail/edit page. On iteration 2+ of
# a foreach the body re-enters from there — where the first step's search/filter control does not
# exist — so it has nowhere to act (live 185: function searched the next parent SKU while still on
# the prior parent's edit page). Prepend an arrival that returns to the list page first.
#
# The instruction is a LINEAR step, not a branch: name = one imperative (go to the list page), SC = a
# definite target STATE (on the list page). It must NOT read "若在编辑页则返回；若已在列表则不操作" —
# that smuggles if/else into a milestone (breaks the FROM→TO-edge + single-page contract and makes
# the selector/planner unable to pick one action: live 185 mis-retrieved Customers knowledge and
# emitted no physical action for the no-op). Idempotency is the MECHANISM's job, not the prose's: the step is
# marked precondition=true, so when the SC already holds (already on the list) the checker passes it
# on frame 1 with no action — iteration 1 / single calls pay nothing — without any conditional text.
_ENTRY_ARRIVAL_NAME = "确保当前处于承载下一步搜索/筛选的列表（数据源）页。"
_ENTRY_ARRIVAL_SC = (
    "当前处于带搜索框与结果表格的列表/搜索（数据源）页，而非某条记录的编辑/详情表单页。"
)


def _first_run_and_later_nav(body: list[Stmt]) -> tuple[Optional[Run], bool]:
    first: Optional[Run] = None
    later_nav = False
    for s in body:
        if isinstance(s, Run):
            if first is None:
                first = s
            elif s.kind == "navigation":
                later_nav = True
    return first, later_nav


# A navigation that DRILLS into a record by CLICKING A ROW in the live list (位置相关：must be on the
# list to find the row) — as opposed to (a) a navigation that ARRIVES at a list/page, or (b) a URL
# OPEN that drills by the row's own href (位置无关：works from any page → needs no list arrival). Only
# the click-a-row form needs the deterministic "回列表页" arrival on foreach re-entry.
_RECORD_DRILL_NAME_RE = re.compile(r"那一?行|Edit\b|编辑页|详情页|详情|进入它|打开它|打开该|的\s*Edit", re.IGNORECASE)
# A name that resolves a URL/href via a {…url…}/{url}/{…链接…} template → position-independent open.
_URL_OPEN_NAME_RE = re.compile(r"\{[^{}]*url[^{}]*\}|\{[^{}]*href[^{}]*\}|\{[^{}]*链接[^{}]*\}|\{url\}", re.IGNORECASE)


def _is_url_open(run: Run) -> bool:
    return run.kind == "navigation" and bool(_URL_OPEN_NAME_RE.search(run.name or ""))


def _is_record_drill(run: Run) -> bool:
    # A URL open is position-independent (drills by href, not by clicking a row in the list) → it is
    # NOT a click-a-row drill and needs no list arrival, even though its name may say 详情/打开.
    return (run.kind == "navigation"
            and bool(_RECORD_DRILL_NAME_RE.search(run.name or ""))
            and not _is_url_open(run))


def _maybe_prepend_arrival(body: list[Stmt], *, drill_first_needs_arrival: bool = False) -> list[Stmt]:
    first, later_nav = _first_run_and_later_nav(body)
    if first is None:
        return body
    # (a) filter/action acting on the list, then drilling away (needs a LATER nav, else it stays on
    # one page) — applies to foreach AND function bodies. (b) a navigation that itself drills into a
    # record row: it inherently leaves the list, so iter2+ re-enters from the prior record and needs
    # the arrival — but ONLY for function bodies (the self-first 185 call-per-row shape). foreach
    # bodies that open a detail per row already work without it (the list-traversal re-entry is
    # handled elsewhere) and a forced arrival there would change long-standing behavior, so it's
    # gated by drill_first_needs_arrival. A *pure* arrival nav (确保在列表页) isn't a drill → excluded.
    acts_on_list = first.kind in ("filter", "action")
    drills_record = drill_first_needs_arrival and _is_record_drill(first)
    if not (acts_on_list or drills_record):
        return body
    if acts_on_list and not later_nav:
        return body  # filter/action body that stays on one page → re-entry == entry, no guard needed
    arrival = Run(kind="navigation", precondition=True,
                  name=_ENTRY_ARRIVAL_NAME, success_condition=_ENTRY_ARRIVAL_SC)
    return [arrival, *body]


def insert_loop_entry_arrivals(program: Program) -> Program:
    """Prepend an idempotent "return to the list page" arrival to every loop/function body that opens
    by acting on a list and then drills into a record — see _ENTRY_ARRIVAL_NAME. Returns a NEW Program
    (inputs untouched); idempotent (a body already starting with the navigation arrival is left alone,
    since its first Run is then kind='navigation')."""
    def walk(stmts: list[Stmt]) -> list[Stmt]:
        out: list[Stmt] = []
        for s in stmts:
            if isinstance(s, ForEach):
                out.append(s.model_copy(update={"body": _maybe_prepend_arrival(walk(s.body))}))
            elif isinstance(s, If):
                out.append(s.model_copy(update={"then": walk(s.then), "otherwise": walk(s.otherwise)}))
            else:
                out.append(s)
        return out

    new_funcs = [f.model_copy(update={"body": _maybe_prepend_arrival(walk(f.body), drill_first_needs_arrival=True)})
                 for f in program.functions]
    new_stmts = walk(program.statements)
    return program.model_copy(update={"statements": new_stmts, "functions": new_funcs})


def _func_exit_sc(name: str, funcs: dict, _seen: frozenset = frozenset()) -> str:
    """Exit state of a function = success_condition of the last COMMAND Run in its body (what the
    page looks like when the call returns). Queries (read/data_query) never move the page, so a
    trailing read must not blank the exit state. Used to chain FROM across a Call. Bounded against
    cycles."""
    fn = funcs.get(name)
    if fn is None or name in _seen:
        return ""
    last_sc = ""
    for s in fn.body:
        if isinstance(s, Run):  # interactive-only by type now; queries never move the page
            last_sc = s.success_condition
        elif isinstance(s, Call):
            last_sc = _func_exit_sc(s.func, funcs, _seen | {name})
    return last_sc


def _chain_block(stmts: list[Stmt], entry_sc: str, funcs: dict) -> list[Stmt]:
    """Walk one linear block, setting each Run.from_state := the success_condition of the Run that
    executes just before it (FROM[i] := TO[i-1]). Compute doesn't change the page (FROM carries
    through); a Call advances FROM to the called function's exit state; If/ForEach recurse with the
    current FROM as their branch/body entry but leave FROM unchanged afterwards (branch/loop end is
    ambiguous — conservative)."""
    out: list[Stmt] = []
    prev = entry_sc
    for s in stmts:
        if isinstance(s, (Read, Query)):
            # Non-interactive pure query: consumes the current frame/table snapshot, never
            # touches the UI, page unchanged → FROM carries through, same as Compute.
            # (Treating it as an ordinary Run broke the chain: a data_query typically has NO
            # success_condition, so the NEXT UI run's from_state went blank.)
            out.append(s)
        elif isinstance(s, Run):
            out.append(s.model_copy(update={"from_state": prev}))
            prev = s.success_condition
        elif isinstance(s, Compute):
            out.append(s)  # pure derivation, page unchanged → FROM carries through
        elif isinstance(s, Call):
            out.append(s)
            prev = _func_exit_sc(s.func, funcs)
        elif isinstance(s, If):
            out.append(s.model_copy(update={
                "then": _chain_block(s.then, prev, funcs),
                "otherwise": _chain_block(s.otherwise, prev, funcs),
            }))
        elif isinstance(s, ForEach):
            out.append(s.model_copy(update={"body": _chain_block(s.body, prev, funcs)}))
        else:
            out.append(s)
    return out


def chain_from_states(program: Program) -> Program:
    """Populate every Run.from_state with the EXIT state of the milestone before it (FROM[i] :=
    TO[i-1]) — see Run.from_state. The chain restarts at each block boundary (main / each function
    body / inside a loop or branch): a block's first step has from_state="" because its entry is the
    call-site / loop-carry state (a known SET the continuity prompt handles), not a single prior SC.
    Returns a NEW Program (inputs untouched); idempotent."""
    funcs = {f.name: f for f in program.functions}
    new_funcs = [f.model_copy(update={"body": _chain_block(f.body, "", funcs)})
                 for f in program.functions]
    new_stmts = _chain_block(program.statements, "", funcs)
    return program.model_copy(update={"statements": new_stmts, "functions": new_funcs})
