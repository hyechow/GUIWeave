"""Deterministic execution preflight checks for benchmark orchestration.

The compiler owns DSL shape and intent-contract validation. This module only
checks whether an already-compiled program is worth executing, without repeating
compiler validation or depending on router state.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from .program import Call, Finish, ForEach, If, Program, Run, RunLike, Stmt


IssueSeverity = Literal["error", "warning"]


class OrchestrationPreflightIssue(BaseModel):
    code: str
    severity: IssueSeverity = "error"
    message: str
    evidence: list[str] = Field(default_factory=list)


class OrchestrationPreflightResult(BaseModel):
    ok: bool
    issues: list[OrchestrationPreflightIssue] = Field(default_factory=list)

    @property
    def blocking_issues(self) -> list[OrchestrationPreflightIssue]:
        return [issue for issue in self.issues if issue.severity == "error"]


_ANSWER_INTENT_MARKERS = (
    "what",
    "which",
    "who",
    "when",
    "where",
    "how many",
    "how much",
    "list",
    "give me",
    "get",
    "find",
    "show",
    "tell me",
    "return",
    "report",
    "retrieve",
    "count",
    "number",
    "average",
    "minimum",
    "maximum",
    "highest",
    "lowest",
    "top ",
    "多少",
    "几个",
    "哪",
    "谁",
    "列出",
    "告诉",
    "返回",
    "查询",
    "统计",
    "数量",
    "最大",
    "最小",
    "最高",
    "最低",
    "前",
)


def validate_orchestration_preflight(
    goal: str,
    program: Program,
) -> OrchestrationPreflightResult:
    """Check whether a decomposed program is worth executing.

    These checks deliberately stay deterministic. They catch high-confidence
    intent loss before benchmark execution, without adding another LLM call to the
    critical path.
    """

    issues: list[OrchestrationPreflightIssue] = []
    runs = list(_iter_runs(program.statements))
    foreach_stmts = list(_iter_foreaches(program.statements))
    calls = list(_iter_calls(program.statements))
    finishes = list(_iter_finishes(program.statements))
    answer_intent = _looks_like_answer_intent(goal)

    if not program.statements:
        issues.append(
            OrchestrationPreflightIssue(
                code="ORCH_EMPTY_PROGRAM",
                message="Decompose produced an empty program.",
            )
        )
    if not runs and not foreach_stmts and not calls:
        issues.append(
            OrchestrationPreflightIssue(
                code="ORCH_NO_EXECUTABLE_WORK",
                message="Program has no run, foreach, or call statement to execute.",
            )
        )

    # WARNING, not blocking: `answer_intent` is a fuzzy keyword heuristic that can't tell a
    # retrieval ("show me the count" → needs a result + finish) from a navigate/show-report task
    # ("Show the sales order report" / "Create an orders report" → nav-submit, scored by
    # NetworkEvent, legitimately has NO finish/returns — see the navigate-submit dispatch gate). And
    # decomposer rule 7 makes finish optional. Blocking here would abort currently-green report tasks
    # (707/708/709). Surface it as a warning; only the router-coverage checks below hard-block.
    if answer_intent and not _has_result_source(runs, foreach_stmts):
        issues.append(
            OrchestrationPreflightIssue(
                code="ORCH_ANSWER_WITHOUT_RESULT_SOURCE",
                severity="warning",
                message="Goal looks like a retrieval/answer task, but the program has no returns, foreach returns, or data_query result.",
                evidence=[_run_summary(r) for r in runs[:8]],
            )
        )
    if answer_intent and not finishes:
        issues.append(
            OrchestrationPreflightIssue(
                code="ORCH_ANSWER_WITHOUT_FINISH",
                severity="warning",
                message="Goal looks like a retrieval/answer task, but the program has no finish statement.",
            )
        )

    issues.extend(_check_purity_discipline(runs))

    ok = not any(issue.severity == "error" for issue in issues)
    return OrchestrationPreflightResult(ok=ok, issues=issues)


# ── 执行模式纪律（交互/非交互边界；脚本生成视角的 lint）───────────────────────────────
# A precondition is an ensure-state GATE: the engine rewrites its success_condition to the
# generic gate (normalize_precondition_gates), so any returns hanging on it would be read off
# whatever frame satisfies that generic gate — a recipe for empty/garbage returns — and sql on
# it is a category error. A query (read/data_query) is a NON-INTERACTIVE primitive that cannot
# touch the UI: a mutation verb in its name means the decomposer wrote an interactive step as a
# query statement, and the "action" would silently never happen.

_MUTATION_VERB_RE = re.compile(
    r"点击|填写|填入|输入|提交|创建|新建|删除|保存|设置|勾选|切换|清除|拖动|滚动|展开|"
    r"\bclick\b|\bsubmit\b|\bcreate\b|\bdelete\b|\bsave\b|\bfill\b|\btoggle\b|\bdrag\b",
    re.IGNORECASE,
)


def _check_purity_discipline(runs: list[Run]) -> list[OrchestrationPreflightIssue]:
    issues: list[OrchestrationPreflightIssue] = []
    for run in runs:
        if isinstance(run, Run) and run.precondition and run.returns:
            issues.append(
                OrchestrationPreflightIssue(
                    code="ORCH_PRECONDITION_IMPURE",
                    message=(
                        "A precondition run must be a pure ensure-state gate: its success_condition "
                        "is rewritten to the generic gate, so returns/sql attached to it read the "
                        "wrong frame. Move the read/query to its own step after the precondition."
                    ),
                    evidence=[_run_summary(run)],
                )
            )
        if run.is_query and _MUTATION_VERB_RE.search(run.name or ""):
            issues.append(
                OrchestrationPreflightIssue(
                    code="ORCH_QUERY_WITH_MUTATION_VERB",
                    severity="warning",
                    message=(
                        "A read/data_query is a pure query primitive — it cannot click, fill, or "
                        "submit anything; the mutation implied by its name would silently never "
                        "happen. Author the mutation as a navigation/filter/action run (optionally "
                        "with returns), then query."
                    ),
                    evidence=[_run_summary(run)],
                )
            )
    return issues


def _iter_runs(stmts: list[Stmt]) -> list[RunLike]:
    out: list[RunLike] = []
    for stmt in stmts:
        if isinstance(stmt, RunLike):
            out.append(stmt)
        elif isinstance(stmt, If):
            out.extend(_iter_runs(stmt.then))
            out.extend(_iter_runs(stmt.otherwise))
        elif isinstance(stmt, ForEach):
            out.extend(_iter_runs(stmt.body))
    return out


def _iter_foreaches(stmts: list[Stmt]) -> list[ForEach]:
    out: list[ForEach] = []
    for stmt in stmts:
        if isinstance(stmt, ForEach):
            out.append(stmt)
            out.extend(_iter_foreaches(stmt.body))
        elif isinstance(stmt, If):
            out.extend(_iter_foreaches(stmt.then))
            out.extend(_iter_foreaches(stmt.otherwise))
    return out


def _iter_finishes(stmts: list[Stmt]) -> list[Finish]:
    out: list[Finish] = []
    for stmt in stmts:
        if isinstance(stmt, Finish):
            out.append(stmt)
        elif isinstance(stmt, If):
            out.extend(_iter_finishes(stmt.then))
            out.extend(_iter_finishes(stmt.otherwise))
        elif isinstance(stmt, ForEach):
            out.extend(_iter_finishes(stmt.body))
    return out


def _iter_calls(stmts: list[Stmt]) -> list[Call]:
    out: list[Call] = []
    for stmt in stmts:
        if isinstance(stmt, Call):
            out.append(stmt)
        elif isinstance(stmt, If):
            out.extend(_iter_calls(stmt.then))
            out.extend(_iter_calls(stmt.otherwise))
        elif isinstance(stmt, ForEach):
            out.extend(_iter_calls(stmt.body))
    return out


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def _looks_like_answer_intent(goal: str) -> bool:
    text = _norm(goal)
    return any(marker in text for marker in _ANSWER_INTENT_MARKERS)


def _has_result_source(runs: list[Run], foreaches: list[ForEach]) -> bool:
    return (
        any(r.returns or r.kind == "data_query" for r in runs)
        or any(f.returns or f.row_fields or f.output_fields for f in foreaches)
    )


def _run_summary(run: Run) -> str:
    suffix = f" returns={run.returns}" if run.returns else ""
    return f"{run.kind}: {run.name}{suffix}"
