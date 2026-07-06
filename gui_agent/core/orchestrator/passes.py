"""Compilation passes: AST-level normalizations over a decomposed DSL Program.

The orchestrator is a compiler frontend (decomposer) → these deterministic passes (compiler
middle-end) → validator/preflight (type-check/lint). Each pass takes a Program and returns a NEW
Program (inputs untouched); all are idempotent. Run order lives in decomposer.to_program:
collapse_foreach_enrichment_passes → insert_loop_entry_arrivals → normalize_confirm_read_gates /
normalize_precondition_gates → chain_from_states. Marshalling into the executor's Milestone format
is NOT here — that is the FFI boundary and lives in callframe.py.
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
# A UI milestone whose result must be read should carry that read as its return contract,
# not as the next UI action. Older decompositions may still produce the legacy shape
# action/filter/navigation -> scalar read; normalize it into one UI Run with
# returns/read_spec so the engine extracts structured values from the action's completion
# frame. For action/filter triggers we also rewrite the gate to "dispatched/responded" so
# the checker does not re-adjudicate the result value that structured_read owns.
#
# `data_query` is deliberately excluded: it analyzes the current structured table snapshot, so
# the preceding UI milestone must still verify the page data source is in the intended
# filter/search/sort/scope state before SQL runs.

_DISPATCH_GATE_TMPL = (
    "已执行「{name}」：动作已发出且界面给出响应"
    "（出现提示/结果区/列表更新/页面跳转/进入加载，任一即可）；"
    "本步不判定结果取值（checker 只判动作响应），"
    "具体结果由本步完成帧的结构化返回值读取判定。"
)

# Navigate/show tasks (no returns / data_query anywhere) end in a terminal submit whose only
# purpose is to NAVIGATE to a destination/render page — e.g. "click Show Report" lands on
# Magento's `…/reports/.../filter/<base64>/` render URL. Without returns, the confirm-read gate
# above never fires, so the LLM checker is left to adjudicate "did we arrive?" — and it misreads
# Magento render URLs that still contain "filter" / keep the submit button visible as "not yet
# submitted", looping the same click. The submit's effect is a navigation, which url_changed
# answers deterministically; gate it like a dispatch gate so the conclusive url_changed marks it
# done and the LLM checker (and its false "filter == config page" prior) is bypassed. Carries the
# dispatch-gate marker so is_dispatch_gate_sc() recognizes it.
_NAV_SUBMIT_GATE_TMPL = (
    "已执行「{name}」：动作已发出且界面给出响应"
    "（页面跳转/URL 变化/出现结果区或加载，任一即可）；"
    "本步是纯导航/展示意图的终态，只判动作是否已触发页面跳转，"
    "不要求出现具体数据行/统计表（报表/结果可能为空）。"
)


# A returns-bearing ACTION that fills form fields before its terminal save is a COMPOUND form op,
# not a single dispatch. Its FIRST url_changed is opening/navigating the form (e.g. list → …/new/),
# which would trip the dispatch gate before any field is filled or the save fires — the milestone
# is marked done on the empty form and the returns read the unsaved page (WebArena 701
# "点击 Add New Rule 并填写规则信息…" → 创建状态=失败). Give it a real "filled + saved"
# success_condition instead of the dispatch gate, so the checker+planner drive fill→save→confirm and
# the milestone actually owns the whole single-page create flow.
_FORM_FILL_RE = re.compile(r"填写|填入|录入|输入", re.I)
# The OPEN/CREATE cue is what makes the milestone navigate (list → …/new/) mid-flow, which is the
# intermediate url_change that misfires the dispatch gate. A fill-ONLY milestone (no open cue) does
# not navigate, so the dispatch gate never fires on it — it must NOT get the "…and saved" SC or it
# can never be satisfied by filling (WebArena 702 split plan: the fill-only milestone got the
# saved-SC, forcing a premature save then a re-create loop).
_FORM_OPEN_RE = re.compile(r"add\s+new|新建|创建|新增|添加|进入[^。]{0,6}表单|打开[^。]{0,6}表单", re.I)


def _is_compound_form_fill(name: str) -> bool:
    text = name or ""
    return bool(_FORM_FILL_RE.search(text)) and bool(_FORM_OPEN_RE.search(text))


_FORM_SAVE_SC_TMPL = (
    "「{name}」已完整填写并保存成功：出现保存成功提示，或已跳转到已保存记录/列表页并可见该新记录；"
    "若仍停留在表单页、字段尚未填全、或出现校验/红色错误，则未完成——继续填写剩余字段后保存。"
)


def _trigger_success_condition(run: Run) -> str:
    """Dispatch gate for a single-dispatch trigger; a real filled+saved SC for a compound form-fill/
    create action (so it is not marked done on the intermediate open-form url_change)."""
    if run.kind == "action" and _is_compound_form_fill(run.name):
        return _FORM_SAVE_SC_TMPL.format(name=run.name)
    return _DISPATCH_GATE_TMPL.format(name=run.name)


_CONFIRM_READ_TRIGGER_KINDS = {"action", "filter"}
_RETURN_READ_SOURCE_KINDS = INTERACTIVE_KINDS


def _normalize_stmts(stmts: list[Stmt]) -> list[Stmt]:
    out: list[Stmt] = []
    i = 0
    while i < len(stmts):
        s = stmts[i]
        nxt = stmts[i + 1] if i + 1 < len(stmts) else None
        if (
            isinstance(s, Run)
            and s.kind in _CONFIRM_READ_TRIGGER_KINDS
            and s.returns
        ):
            update = {"success_condition": _trigger_success_condition(s)}
            if s.kind == "filter":
                # A filter with returns is a trigger whose returned values own the count/value
                # judgment. Execute it as an action so the filter checker does not re-judge the
                # same result fields.
                update["kind"] = "action"
            out.append(s.model_copy(update=update))
            i += 1
            continue
        if (
            isinstance(s, Run)
            and isinstance(nxt, Read)
            and s.kind in _RETURN_READ_SOURCE_KINDS
            and nxt.returns
            and (not s.var or not nxt.var or s.var == nxt.var)
        ):
            update = {
                "var": nxt.var or s.var,
                "returns": list(nxt.returns),
                "read_spec": nxt.read_spec,
            }
            if s.kind in _CONFIRM_READ_TRIGGER_KINDS:
                update = {"success_condition": _trigger_success_condition(s)}
                update.update({
                    "var": nxt.var or s.var,
                    "returns": list(nxt.returns),
                    "read_spec": nxt.read_spec,
                })
                if s.kind == "filter":
                    # A filter that is immediately read is a trigger, not a final acceptance
                    # target. Convert it to action so the filter checker doesn't re-judge the
                    # eventual visible value/count; the following read owns that result.
                    update["kind"] = "action"
            out.append(s.model_copy(update=update))
            i += 2
            continue
        if isinstance(s, Run) and s.kind in _CONFIRM_READ_TRIGGER_KINDS:
            if isinstance(nxt, Read):
                update = {"success_condition": _trigger_success_condition(s)}
                if s.kind == "filter":
                    update["kind"] = "action"
                s = s.model_copy(update=update)
            out.append(s)
        elif isinstance(s, If):
            out.append(s.model_copy(update={
                "then": _normalize_stmts(s.then),
                "otherwise": _normalize_stmts(s.otherwise),
            }))
        else:
            out.append(s)
        i += 1
    return out


def _program_is_pure_navigate(stmts: list[Stmt]) -> bool:
    """A navigate/show program: no statement anywhere requests structured data — no Run with
    returns, no data_query, no foreach (collection). Such a task is judged purely by arrival."""
    for s in stmts:
        if isinstance(s, RunLike):
            if s.returns or isinstance(s, Query):
                return False
        elif isinstance(s, ForEach):
            return False
        elif isinstance(s, If):
            if not (_program_is_pure_navigate(s.then) and _program_is_pure_navigate(s.otherwise)):
                return False
    return True


def _has_navigation_run(stmts: list[Stmt]) -> bool:
    """True if any Run (recursively) is a navigation step — i.e. the task genuinely reaches a
    destination page. Distinguishes a reach-then-submit SHOW task (navigation + terminal action,
    e.g. enter report page then Show Report) from a bare action sequence (no navigation), so the
    navigate-submit gate stays scoped to arrival tasks and never touches plain action chains."""
    for s in stmts:
        if isinstance(s, Run) and s.kind == "navigation":
            return True
        if isinstance(s, If) and (_has_navigation_run(s.then) or _has_navigation_run(s.otherwise)):
            return True
        if isinstance(s, ForEach) and _has_navigation_run(s.body):
            return True
    return False


def _gate_terminal_navigate_submit(stmts: list[Stmt]) -> list[Stmt]:
    """For a pure-navigate program, rewrite the LAST top-level action/filter Run's gate to the
    navigate-submit dispatch gate so its conclusive url_changed marks the milestone done (the
    LLM checker is bypassed). Navigation-kind runs keep their own arrival checker untouched."""
    from gui_agent.core.supervisor.milestone.helpers import is_dispatch_gate_sc

    out = list(stmts)
    for i in range(len(out) - 1, -1, -1):
        s = out[i]
        if isinstance(s, Run) and s.kind in _CONFIRM_READ_TRIGGER_KINDS:
            if not is_dispatch_gate_sc(s.success_condition):
                out[i] = s.model_copy(
                    update={"success_condition": _NAV_SUBMIT_GATE_TMPL.format(name=s.name)}
                )
            break
        if isinstance(s, RunLike):  # a navigation/read terminal — leave arrival checking to it
            break
    return out


def normalize_confirm_read_gates(program: Program) -> Program:
    """Normalize legacy action->read pairs into action return contracts.

    Older plans express result extraction as a scalar read Run immediately after a UI Run.
    Newer plans put ``returns``/``read_spec`` on that UI Run directly. This pass makes both
    shapes execute the same way: scalar read pairs are merged into the UI Run when the vars
    are compatible, and action/filter trigger gates are made lenient dispatch gates so the
    checker accepts on response while structured_read owns the returned value. A following
    data_query is not treated as a return read; the preceding UI step must still verify the
    data source state before SQL analyzes it. For pure-navigate programs (no returns/
    data_query/foreach), the terminal submit action is additionally gated as a navigate-submit
    dispatch gate so url_changed deterministically marks arrival (the LLM checker's false
    "render URL still contains 'filter' == not submitted" prior is bypassed). Recurses into
    if-branches; returns a NEW Program (inputs untouched); idempotent."""
    stmts = _normalize_stmts(program.statements)
    if _program_is_pure_navigate(stmts) and _has_navigation_run(stmts):
        stmts = _gate_terminal_navigate_submit(stmts)
    return program.model_copy(update={"statements": stmts})


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
    "而非只在未完成态才出现的中间界面（如登录表单）；"
    "初始若已满足则第一帧即判 done、直接跳过。"
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
    not just login. An already-satisfied precondition is then judged done on frame 1 (no form/data
    antipattern → no stuck). Recurses into if-branches; returns a NEW Program (inputs untouched);
    idempotent. App-specific markers stay in the checker's _check.md, which judges this gate."""
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
    if first.body or first.body_goal or not first.returns:
        return False
    if not second.body or second.body_goal:
        return False
    first_table = first.into or f"{first.var}s"
    if second.over and second.over != first_table:
        return False
    refs = _refs_to_loop_var(second.body, second.var)
    if not refs:
        return False
    return _lower_set(refs).issubset(_lower_set(first.returns))


def _collapse_foreach_stmts(stmts: list[Stmt]) -> list[Stmt]:
    out: list[Stmt] = []
    i = 0
    while i < len(stmts):
        s = stmts[i]
        nxt = stmts[i + 1] if i + 1 < len(stmts) else None
        if isinstance(s, ForEach) and isinstance(nxt, ForEach) and _can_collapse_foreach_pair(s, nxt):
            out.append(nxt.model_copy(update={
                "over": "",
                "target": s.target or nxt.target,
                "returns": list(s.returns),
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
# emitted a [stop] for the no-op). Idempotency is the MECHANISM's job, not the prose's: the step is
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
