"""Deterministic preflight checks for benchmark orchestration.

The normal ``validate_program`` gate protects DSL shape: references resolve, SQL is
syntactically safe, branches read declared fields, and so on. This module is a
separate execution gate for benchmark runs: it checks whether the router's
semantic decisions still appear in the decomposed program before spending turns
on UI execution.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from gui_agent.core.router import IntentResolution

from .program import Call, Finish, ForEach, If, Program, Run, Stmt


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
    *,
    resolution: IntentResolution | None = None,
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
    program_text = _program_text(program)
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

    if resolution is not None:
        issues.extend(_check_router_entity_coverage(resolution, program_text, foreach_stmts, program))

    ok = not any(issue.severity == "error" for issue in issues)
    return OrchestrationPreflightResult(ok=ok, issues=issues)


def _check_router_entity_coverage(
    resolution: IntentResolution,
    program_text: str,
    foreach_stmts: list[ForEach],
    program: Program | None = None,
) -> list[OrchestrationPreflightIssue]:
    def _program_has_if_or_fn_if() -> bool:
        # An If in a called function body also counts as a membership mechanism.
        for fn in (getattr(program, "functions", None) or []):
            for st in fn.body:
                if isinstance(st, If):
                    return True
        return False

    issues: list[OrchestrationPreflightIssue] = []
    for entity in resolution.entities:
        # role=value entities are values to SET (a new rule name, a form scope) — used verbatim,
        # never searched, never iterated. Enforcing lookup-coverage on them produced false blocks
        # on live-green tasks: 703 (rule name "Thanks giving sale" normalized to key "Thanksgiving"
        # → KEY_DROPPED) and 702 ("all registered customers" scope → SET_WITHOUT_FOREACH).
        if str(getattr(entity, "role", "lookup") or "lookup").strip().lower() == "value":
            continue
        mention = str(entity.mention or "").strip()
        search_key = str(entity.search_key or "").strip()
        match_mode = str(entity.match_mode or "").strip().lower()
        cardinality = str(getattr(entity, "cardinality", "") or "").strip().lower()

        mention_present = _contains(program_text, mention)
        key_present = _contains(program_text, search_key)
        key_present_as_own_strategy = key_present
        if mention and search_key and _norm(mention) != _norm(search_key):
            key_present_as_own_strategy = _contains(_remove_norm_phrase(program_text, mention), search_key)

        if mention or search_key:
            if match_mode == "approximate":
                if search_key and not key_present_as_own_strategy:
                    issues.append(
                        OrchestrationPreflightIssue(
                            code="ROUTER_APPROXIMATE_KEY_DROPPED",
                            message=(
                                "Router marked an entity as approximate, but the decomposed program "
                                "does not include its search_key."
                            ),
                            evidence=[f"mention={mention}", f"search_key={search_key}"],
                        )
                    )
                elif not mention_present and not key_present:
                    issues.append(
                        OrchestrationPreflightIssue(
                            code="ROUTER_ENTITY_DROPPED",
                            message="Router extracted an entity that does not appear in the decomposed program.",
                            evidence=[f"mention={mention}", f"search_key={search_key}"],
                        )
                    )
            elif not mention_present and not key_present:
                issues.append(
                    OrchestrationPreflightIssue(
                        code="ROUTER_ENTITY_DROPPED",
                        message="Router extracted an entity that does not appear in the decomposed program.",
                        evidence=[f"mention={mention}", f"search_key={search_key}"],
                    )
                )

        if cardinality == "set" and not foreach_stmts:
            issues.append(
                OrchestrationPreflightIssue(
                    code="ROUTER_SET_ENTITY_WITHOUT_FOREACH",
                    message="Router marked an entity as a set, but the program has no foreach iteration.",
                    evidence=[f"mention={mention}", f"selector={getattr(entity, 'selector', '') or ''}"],
                )
            )
        elif cardinality == "set" and str(getattr(entity, "selector", "") or "").strip():
            # A foreach exists — but does anything actually APPLY the selector? Naming the into
            # table "size28_leggings" is not filtering (live 778 run 235723: foreach over ALL 7
            # Sahara rows straight into a price-cut call — would have mutated the -29- variants and
            # the configurable parent). A membership mechanism is one of: member_desc (selection
            # checkpoint), a body_goal (per-row judgment), or an If inside the body/functions.
            def _has_membership(fe: ForEach) -> bool:
                if (getattr(fe, "member_desc", "") or "").strip() or (fe.body_goal or "").strip():
                    return True
                def _walk(stmts) -> bool:
                    for st in stmts:
                        if isinstance(st, If):
                            return True
                        if isinstance(st, ForEach) and _walk(st.body):
                            return True
                    return False
                return _walk(fe.body)

            def _selector_in_filter_step() -> bool:
                # A UI filter/search step that carries the selector's tokens scopes the collection
                # BEFORE the loop (e.g. "Filter products by size 28") — equally valid membership.
                sel_tokens = [t for t in _norm(getattr(entity, "selector", "")).split() if len(t) >= 2]
                if not sel_tokens or program is None:
                    return False
                for st in _iter_runs(program.statements):
                    if st.kind in ("filter", "navigation"):
                        text = _norm(f"{st.name} {st.success_condition}")
                        if all(t in text for t in sel_tokens):
                            return True
                return False

            if (not any(_has_membership(fe) for fe in foreach_stmts)
                    and not _program_has_if_or_fn_if() and not _selector_in_filter_step()):
                issues.append(
                    OrchestrationPreflightIssue(
                        code="ROUTER_SET_SELECTOR_NOT_APPLIED",
                        message=(
                            "Router marked a set entity with a selector, but no foreach carries a "
                            "membership mechanism (member_desc / body_goal / an If) — the loop would "
                            "act on EVERY collected row, mutating non-members."
                        ),
                        evidence=[f"mention={mention}", f"selector={getattr(entity, 'selector', '')}"],
                    )
                )
    return issues


def _iter_runs(stmts: list[Stmt]) -> list[Run]:
    out: list[Run] = []
    for stmt in stmts:
        if isinstance(stmt, Run):
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


def _program_text(program: Program) -> str:
    return program.model_dump_json(exclude_none=True)


def _contains(haystack: str, needle: str) -> bool:
    if not needle.strip():
        return False
    return _norm(needle) in _norm(haystack)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def _remove_norm_phrase(text: str, phrase: str) -> str:
    return _norm(text).replace(_norm(phrase), " ")


def _looks_like_answer_intent(goal: str) -> bool:
    text = _norm(goal)
    return any(marker in text for marker in _ANSWER_INTENT_MARKERS)


def _has_result_source(runs: list[Run], foreaches: list[ForEach]) -> bool:
    return any(r.returns or r.kind == "data_query" for r in runs) or any(f.returns for f in foreaches)


def _run_summary(run: Run) -> str:
    suffix = f" returns={run.returns}" if run.returns else ""
    return f"{run.kind}: {run.name}{suffix}"
