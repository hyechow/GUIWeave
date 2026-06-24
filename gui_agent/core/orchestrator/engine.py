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

from typing import Literal

from gui_agent.core.schemas import Milestone

from .program import If, Program, Run, Stmt
from .runner import RunResult

# DSL RunKind -> feat-android (kind, completion_strategy).
_KIND_MAP: dict[str, tuple[str, str]] = {
    "navigation": ("navigation", "visible_once"),
    "filter": ("filter", "visible_once"),
    "action": ("action", "visible_once"),
    "read": ("collection", "read_once"),
    "data_query": ("collection", "read_once"),
}


def is_list_read(run: Run) -> bool:
    """A ``read`` run whose result is a runtime-discovered row collection."""
    return run.kind == "read" and bool(getattr(run, "list_read", False)) and bool(run.returns)


def to_milestone(run: Run, index: int) -> Milestone:
    """Build a feat-android Milestone the supervisor can drive from a DSL Run spec.

    `returns` (fields to read) are folded into the description so the read instruction
    targets them; structured {field: value} extraction is a later step (#3)."""
    kind, strategy = _KIND_MAP.get(run.kind, ("action", "visible_once"))
    desc = run.name
    if run.returns:
        desc = f"{run.name}（读取字段：{'、'.join(run.returns)}）"
    success = run.success_condition or f"完成「{run.name}」"
    if is_list_read(run):
        strategy = "react_until_collected"
        success = (
            f"已完整遍历目标集合「{run.name}」：当前页所有行已处理，必要的行详情已读取，"
            "并已翻页/滚动到集合末尾；不是只读取当前可见帧。"
        )
    return Milestone(
        id=run.var or f"m{index}_{run.kind}",
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
    structured {field: value} for a scalar read; `rows` is the LIST form for a list_read (one dict per
    row, what a foreach iterates); other milestones pass none."""
    return RunResult(
        completed=completed,
        failed=not completed,
        reads=dict(reads) if reads else {},
        rows=list(rows) if rows else [],
        summary=summary,
        evidence=list(notes),
    )


# ── confirm-read structural backstop (L2) ────────────────────────────────────────────
# A milestone whose result is confirmed by a following read should be ACCEPTED on
# "the action/filter fired", never re-adjudicated by the per-milestone checker — that checker is
# known to thrash on freshly-shown verdicts (20260615_100753: it saw a green ✓, re-clicked
# 检测, then hallucinated the same ✓ as gray ?, burning 2 frames; WebArena 15: a grid showed
# "2 records found" after Review=best but the filter checker kept re-judging visible rows).
# The decomposer prompt (L1) *asks* for a dispatch-form success_condition; this pass
# *guarantees* it. The signal is purely structural — action/filter Run immediately followed
# by a read Run is the confirm-read shape — so we never string-match the gate's meaning.
# Generic over create / submit / delete / send / detect / apply-filter: any trigger→read
# adjacency. See structured_read / the read primitive for who owns the result judgment instead.
# `data_query` is deliberately excluded: it analyzes the current structured table snapshot, so
# the preceding UI milestone must still verify the page data source is in the intended
# filter/search/sort/scope state before SQL runs.

_DISPATCH_GATE_TMPL = (
    "已执行「{name}」：动作已发出且界面给出响应"
    "（出现提示/结果区/列表更新/页面跳转/进入加载，任一即可）；"
    "本步不判定结果取值，具体结果由下一步读取判定。"
)


_CONFIRM_READ_TRIGGER_KINDS = {"action", "filter"}
_CONFIRM_READ_TARGET_KINDS = {"read"}


def _normalize_stmts(stmts: list[Stmt]) -> list[Stmt]:
    out: list[Stmt] = []
    n = len(stmts)
    for i, s in enumerate(stmts):
        if isinstance(s, Run) and s.kind in _CONFIRM_READ_TRIGGER_KINDS:
            nxt = stmts[i + 1] if i + 1 < n else None
            if isinstance(nxt, Run) and nxt.kind in _CONFIRM_READ_TARGET_KINDS:
                update = {"success_condition": _DISPATCH_GATE_TMPL.format(name=s.name)}
                if s.kind == "filter":
                    # A filter that is immediately read is a trigger, not a final acceptance
                    # target. Convert it to action so the filter checker doesn't re-judge the
                    # eventual visible value/count; the following read owns that result.
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
    return out


def normalize_confirm_read_gates(program: Program) -> Program:
    """Rewrite every confirm-read-backed trigger's gate to a lenient DISPATCH gate.

    An action/filter Run immediately followed by a read Run (the confirm-read shape) gets its
    success_condition replaced so the per-milestone checker accepts on "the action fired
    and the page responded" and never adjudicates the result the read owns. A filter in this
    shape is converted to action for execution, because the following read owns the visible
    state/value. A following data_query is not treated as confirm-read; the preceding UI step
    must still verify the data source state before SQL analyzes it. Recurses into
    if-branches; returns a NEW Program (inputs untouched); idempotent. This is the structural
    guarantee behind the decomposer's L1 prompt nudge — independent of how the LLM phrased
    the gate, so it covers create/submit/delete/send/detect/apply-filter uniformly."""
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
