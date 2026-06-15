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
}


def to_milestone(run: Run, index: int) -> Milestone:
    """Build a feat-android Milestone the supervisor can drive from a DSL Run spec.

    `returns` (fields to read) are folded into the description so the read instruction
    targets them; structured {field: value} extraction is a later step (#3)."""
    kind, strategy = _KIND_MAP.get(run.kind, ("action", "visible_once"))
    desc = run.name
    if run.returns:
        desc = f"{run.name}（读取字段：{'、'.join(run.returns)}）"
    return Milestone(
        id=run.var or f"m{index}_{run.kind}",
        name=run.name,
        description=desc,
        success_condition=run.success_condition or f"完成「{run.name}」",
        kind=kind,  # type: ignore[arg-type]  # validated against MilestoneKind Literal
        completion_strategy=strategy,  # type: ignore[arg-type]
    )


def task_type_for(run: Run) -> Literal["action", "analysis"]:
    """A read Run -> 'analysis' so the supervisor's task_type-gated reader actually reads
    (feat-android gates reading on task_type; see policy._ctx / _default_read_instruction)."""
    return "analysis" if run.kind == "read" else "action"


def package_result(
    run: Run, *, completed: bool, summary: str, notes: list[str],
    reads: dict[str, str] | None = None,
) -> RunResult:
    """Package a finished milestone's loop state into the RunResult contract. `reads` is the
    structured {field: value} extracted from the result frame for a read milestone (see
    orchestrator.structured_read); other milestones pass none."""
    return RunResult(
        completed=completed,
        failed=not completed,
        reads=dict(reads) if reads else {},
        summary=summary,
        evidence=list(notes),
    )


# ── confirm-read structural backstop (L2) ────────────────────────────────────────────
# An action milestone whose result is confirmed by a following read should be ACCEPTED on
# "the action fired", never re-adjudicated by the per-milestone checker — that checker is
# known to thrash on freshly-shown verdicts (20260615_100753: it saw a green ✓, re-clicked
# 检测, then hallucinated the same ✓ as gray ?, burning 2 frames). The decomposer prompt (L1)
# *asks* for a dispatch-form success_condition; this pass *guarantees* it. The signal is
# purely structural — an action Run immediately followed by a read Run is the confirm-read
# shape (decomposer rule 6) — so we never string-match the gate's meaning. Generic over
# create / submit / delete / send / detect: any action→read adjacency. See structured_read /
# the read primitive for who owns the result judgment instead.

_DISPATCH_GATE_TMPL = (
    "已执行「{name}」：动作已发出且界面给出响应"
    "（出现提示/结果区/列表更新/页面跳转/进入加载，任一即可）；"
    "本步不判定结果取值，具体结果由下一步读取判定。"
)


def _normalize_stmts(stmts: list[Stmt]) -> list[Stmt]:
    out: list[Stmt] = []
    n = len(stmts)
    for i, s in enumerate(stmts):
        if isinstance(s, Run) and s.kind == "action":
            nxt = stmts[i + 1] if i + 1 < n else None
            if isinstance(nxt, Run) and nxt.kind == "read":
                s = s.model_copy(update={"success_condition": _DISPATCH_GATE_TMPL.format(name=s.name)})
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
    """Rewrite every confirm-read-backed action's success_condition to a lenient DISPATCH gate.

    An action Run immediately followed by a read Run (the confirm-read shape) gets its
    success_condition replaced so the per-milestone checker accepts on "the action fired
    and the page responded" and never adjudicates the result the read owns. Recurses into
    if-branches; returns a NEW Program (inputs untouched); idempotent. This is the structural
    guarantee behind the decomposer's L1 prompt nudge — independent of how the LLM phrased
    the gate, so it covers create/submit/delete/send/detect uniformly."""
    return program.model_copy(update={"statements": _normalize_stmts(program.statements)})
