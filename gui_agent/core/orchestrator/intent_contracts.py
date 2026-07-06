"""Intent coverage contracts for orchestrator programs.

This layer sits between pure DSL validation and final preflight. It checks whether a
compiled Program still covers the router's task semantics (entities, approximate keys,
set cardinality, selectors, and entity-scoped foreach summaries). It deliberately does
not validate Python/SQL/field data-flow shape; that remains validator.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from gui_agent.core.router import IntentResolution

from .program import ForEach, If, Program, Query, Run, RunLike, Stmt

IssueSeverity = Literal["error", "warning"]


@dataclass(frozen=True)
class IntentContractIssue:
    code: str
    message: str
    severity: IssueSeverity = "error"
    evidence: tuple[str, ...] = field(default_factory=tuple)


def validate_intent_contracts(
    program: Program,
    resolution: IntentResolution | None,
) -> list[IntentContractIssue]:
    """Validate Program + IntentResolution coverage contracts."""

    if resolution is None:
        return []
    issues: list[IntentContractIssue] = []
    issues.extend(_check_router_entity_coverage(program, resolution))
    issues.extend(_check_entity_scope_predicates(program, resolution))
    return issues


def _check_router_entity_coverage(program: Program, resolution: IntentResolution) -> list[IntentContractIssue]:
    program_text = _program_text(program)
    foreach_stmts = _iter_foreaches(program.statements)
    issues: list[IntentContractIssue] = []

    for entity in resolution.entities:
        # role=value entities are values to SET (a new rule name, a form scope) — used verbatim,
        # never searched, never iterated. Coverage checks on them false-block create/update tasks.
        if _entity_role(entity) == "value":
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
                    issues.append(IntentContractIssue(
                        code="ROUTER_APPROXIMATE_KEY_DROPPED",
                        message=(
                            "Router marked an entity as approximate, but the decomposed program "
                            "does not include its search_key."
                        ),
                        evidence=(f"mention={mention}", f"search_key={search_key}"),
                    ))
                elif not mention_present and not key_present:
                    issues.append(IntentContractIssue(
                        code="ROUTER_ENTITY_DROPPED",
                        message="Router extracted an entity that does not appear in the decomposed program.",
                        evidence=(f"mention={mention}", f"search_key={search_key}"),
                    ))
            elif not mention_present and not key_present:
                issues.append(IntentContractIssue(
                    code="ROUTER_ENTITY_DROPPED",
                    message="Router extracted an entity that does not appear in the decomposed program.",
                    evidence=(f"mention={mention}", f"search_key={search_key}"),
                ))

        if cardinality == "set" and not foreach_stmts:
            issues.append(IntentContractIssue(
                code="ROUTER_SET_ENTITY_WITHOUT_FOREACH",
                message="Router marked an entity as a set, but the program has no foreach iteration.",
                evidence=(f"mention={mention}", f"selector={getattr(entity, 'selector', '') or ''}"),
            ))
        elif cardinality == "set" and str(getattr(entity, "selector", "") or "").strip():
            selector = str(getattr(entity, "selector", "") or "").strip()
            if (
                not any(_foreach_has_membership(fe) for fe in foreach_stmts)
                and not _program_has_if(program)
                and not _selector_in_filter_or_navigation_step(program, selector)
            ):
                issues.append(IntentContractIssue(
                    code="ROUTER_SET_SELECTOR_NOT_APPLIED",
                    message=(
                        "Router marked a set entity with a selector, but no foreach carries a "
                        "membership mechanism (member_desc / body_goal / an If) — the loop would "
                        "act on EVERY collected row, mutating non-members."
                    ),
                    evidence=(f"mention={mention}", f"selector={selector}"),
                ))
    return issues


def _check_entity_scope_predicates(program: Program, resolution: IntentResolution) -> list[IntentContractIssue]:
    entities = getattr(resolution, "entities", None) or []
    if not entities:
        return []

    into_tables: set[str] = set()
    scoped_queries: list[Query] = []
    ui_text_parts: list[str] = []

    def _collect(stmts: list[Stmt]) -> None:
        for s in stmts:
            if isinstance(s, ForEach):
                into_tables.add((s.into or f"{s.var}s").strip().lower())
                ui_text_parts.extend([s.target or "", s.member_desc or "", s.body_goal or ""])
                _collect(s.body)
            elif isinstance(s, Query):
                scoped_queries.append(s)
            elif isinstance(s, Run):
                ui_text_parts.extend([s.name or "", s.success_condition or ""])
            elif isinstance(s, If):
                _collect(s.then)
                _collect(s.otherwise)

    _collect(program.statements)
    for fn in getattr(program, "functions", None) or []:
        _collect(fn.body)
    if not into_tables:
        return []

    into_queries = [
        q for q in scoped_queries
        if (_sql_referenced_tables(q.sql or "") - _sql_cte_names(q.sql or "")) & into_tables
    ]
    if not into_queries:
        return []

    issues: list[IntentContractIssue] = []
    ui_text = " ".join(ui_text_parts).lower()
    for entity in entities:
        if _entity_role(entity) == "value":
            continue
        keys = [str(k).strip() for k in (getattr(entity, "search_key", ""), getattr(entity, "mention", ""))
                if str(k or "").strip()]
        if not keys:
            continue
        keys_lower = [k.lower() for k in keys]
        if not any(k in ui_text for k in keys_lower):
            continue
        if any(k in (q.sql or "").lower() for q in into_queries for k in keys_lower):
            continue
        issues.append(IntentContractIssue(
            code="ENTITY_SCOPE_PREDICATE_MISSING",
            message=(
                f"任务把记录圈定到实体「{keys[0]}」，但查询 foreach 汇总表的 data_query 没带实体范围谓词——"
                f"上游筛选可能被误触/Reset/翻页弄丢，会把别的实体的行当答案返回。请让 foreach row_fields 顺手"
                f"包含实体标识列，并在该 data_query 的 SQL 里加 `WHERE <实体列> LIKE '%{keys[0]}%' AND ...`；"
                "「前面已筛过」不构成省略理由（那可能是已失效的旧状态）。"
            ),
        ))
    return issues


def _entity_role(entity: object) -> str:
    return str(getattr(entity, "role", "lookup") or "lookup").strip().lower()


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


def _block_has_if(stmts: list[Stmt]) -> bool:
    for stmt in stmts:
        if isinstance(stmt, If):
            return True
        if isinstance(stmt, ForEach) and _block_has_if(stmt.body):
            return True
    return False


def _program_has_if(program: Program) -> bool:
    return _block_has_if(program.statements) or any(
        _block_has_if(fn.body) for fn in (getattr(program, "functions", None) or [])
    )


def _foreach_has_membership(loop: ForEach) -> bool:
    return bool(
        (getattr(loop, "member_desc", "") or "").strip()
        or (loop.body_goal or "").strip()
        or _block_has_if(loop.body)
    )


def _selector_in_filter_or_navigation_step(program: Program, selector: str) -> bool:
    tokens = [t for t in _norm(selector).split() if len(t) >= 2]
    if not tokens:
        return False
    for st in _iter_runs(program.statements):
        if st.kind in ("filter", "navigation"):
            text = _norm(f"{st.name} {st.success_condition}")
            if all(t in text for t in tokens):
                return True
    for fn in (getattr(program, "functions", None) or []):
        for st in _iter_runs(fn.body):
            if st.kind in ("filter", "navigation"):
                text = _norm(f"{st.name} {st.success_condition}")
                if all(t in text for t in tokens):
                    return True
    return False


def _sql_cte_names(sql: str) -> set[str]:
    text = sql or ""
    if not re.match(r"^\s*with\b", text, flags=re.I):
        return set()
    return {
        raw.lower()
        for raw in re.findall(r"(?:\bwith\b|,)\s*([A-Za-z_][A-Za-z0-9_]*)\s+as\s*\(", text, flags=re.I)
    }


def _sql_referenced_tables(sql: str) -> set[str]:
    return {
        raw.lower()
        for raw in re.findall(r"\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_]*)\b", sql or "", flags=re.I)
    }
