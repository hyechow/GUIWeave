"""Bridge between the DSL orchestrator and the existing per-milestone executor (agent_loop).

The agent_loop IS the runner; this module only TRANSLATES between the DSL's Run/RunResult
and the supervisor's Milestone, and packages a finished milestone's loop state into a
RunResult. The agent_loop drives Interpreter.steps(), reseeds the supervisor per Run via
to_milestone()/task_type_for(), and on milestone-done calls package_result().

Branch note: on feat-android there is no `inspect`/`read_spec` (that's the parked
feat-read-spec). So a `read` Run maps to collection + read_once and reads come back as
unstructured content_notes text — structured {field: value} extraction is step #3.
"""

from __future__ import annotations

import hashlib
import re
from typing import Literal

from gui_agent.core.schemas import Milestone

from .program import ForEach, If, Program, Run, Stmt
from .runner import RunResult

# DSL RunKind -> feat-android (kind, completion_strategy).
_KIND_MAP: dict[str, tuple[str, str]] = {
    "navigation": ("navigation", "visible_once"),
    "filter": ("filter", "visible_once"),
    "action": ("action", "visible_once"),
    "read": ("collection", "read_once"),
    "data_query": ("collection", "read_once"),
}



def _milestone_id(run: Run, index: int) -> str:
    base = run.var or f"m{index}_{run.kind}"
    if run.var and run.kind in _RETURN_READ_SOURCE_KINDS and run.returns:
        slug = re.sub(r"[^a-zA-Z0-9]+", "_", run.name).strip("_")[:32]
        digest = hashlib.sha1(run.name.encode("utf-8")).hexdigest()[:8]
        return f"{base}_{slug or digest}_{digest}"
    return base


def to_milestone(run: Run, index: int) -> Milestone:
    """Build a feat-android Milestone the supervisor can drive from a DSL Run spec.

    `returns` (fields to read) are folded into the description so the read instruction
    targets them; structured {field: value} extraction is a later step (#3)."""
    kind, strategy = _KIND_MAP.get(run.kind, ("action", "visible_once"))
    desc = run.name
    if run.returns:
        desc = f"{run.name}（读取字段：{'、'.join(run.returns)}）"
    success = run.success_condition or f"完成「{run.name}」"
    return Milestone(
        id=_milestone_id(run, index),
        name=run.name,
        description=desc,
        success_condition=success,
        kind=kind,  # type: ignore[arg-type]  # validated against MilestoneKind Literal
        completion_strategy=strategy,  # type: ignore[arg-type]
    )


def task_type_for(run: Run) -> Literal["action", "analysis"]:
    """A read Run -> 'analysis' so the supervisor's task_type-gated reader actually reads
    (feat-android gates reading on task_type; see policy._ctx / _default_read_instruction)."""
    return "analysis" if run.kind in {"read", "data_query"} else "action"


def package_result(
    run: Run, *, completed: bool, summary: str, notes: list[str],
    reads: dict[str, str] | None = None,
    rows: list[dict[str, str]] | None = None,
) -> RunResult:
    """Package a finished milestone's loop state into the RunResult contract. `reads` is the
    structured {field: value} for a scalar read; `rows` is the LIST form for a foreach-accumulated
    table (one dict per row); other milestones pass none."""
    return RunResult(
        completed=completed,
        failed=not completed,
        reads=dict(reads) if reads else {},
        rows=list(rows) if rows else [],
        summary=summary,
        evidence=list(notes),
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


_CONFIRM_READ_TRIGGER_KINDS = {"action", "filter"}
_RETURN_READ_SOURCE_KINDS = {"navigation", "filter", "action"}
_RETURN_READ_TARGET_KINDS = {"read"}


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
            update = {"success_condition": _DISPATCH_GATE_TMPL.format(name=s.name)}
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
            and isinstance(nxt, Run)
            and s.kind in _RETURN_READ_SOURCE_KINDS
            and nxt.kind in _RETURN_READ_TARGET_KINDS
            and nxt.returns
            and (not s.var or not nxt.var or s.var == nxt.var)
        ):
            update = {
                "var": nxt.var or s.var,
                "returns": list(nxt.returns),
                "read_spec": nxt.read_spec,
            }
            if s.kind in _CONFIRM_READ_TRIGGER_KINDS:
                update = {"success_condition": _DISPATCH_GATE_TMPL.format(name=s.name)}
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
            if isinstance(nxt, Run) and nxt.kind in _RETURN_READ_TARGET_KINDS:
                update = {"success_condition": _DISPATCH_GATE_TMPL.format(name=s.name)}
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
        if isinstance(s, Run):
            if s.returns or s.kind == "data_query":
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
        if isinstance(s, Run):  # a navigation/read terminal — leave arrival checking to it
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
