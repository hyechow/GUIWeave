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
from gui_agent.core.schemas import target_value_options

from .program import Call, Compute, ForEach, If, Program, Query, Run, RunLike, Stmt

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
    issues.extend(_check_value_entity_consumption(program, resolution))
    issues.extend(_check_entity_scope_predicates(program, resolution))
    return issues


def _check_value_entity_consumption(
    program: Program,
    resolution: IntentResolution,
) -> list[IntentContractIssue]:
    """Every router value must reach an executable consumer, not merely survive in the goal.

    Values are not lookup entities, so they intentionally skip retrieval coverage. They are still
    part of the task contract: dropping a color, message, target state, rule name, or form scope can
    produce a structurally valid program that mutates the wrong state. Only mutation/data-flow nodes
    count as consumers; navigation, filters, finish prose, and Program.goal do not.
    """

    consumer_text = _value_consumer_text(program)
    issues: list[IntentContractIssue] = []
    for entity in resolution.entities:
        if _entity_role(entity) != "value":
            continue
        members = target_value_options(entity.value_members)
        if len(members) > 1:
            missing = tuple(value for value in members if not _contains(consumer_text, value))
            if not missing:
                continue
            value = ", ".join(missing)
        else:
            mention = str(getattr(entity, "mention", "") or "").strip()
            search_key = str(getattr(entity, "search_key", "") or "").strip()
            if not mention and not search_key:
                continue
            if _contains(consumer_text, mention) or _contains(consumer_text, search_key):
                continue
            value = mention or search_key
        issues.append(IntentContractIssue(
            code="ROUTER_VALUE_DROPPED",
            message=(
                f"Router marked「{value}」as role=value, but no action, compute, call argument, or "
                "foreach body_goal consumes that value. A value appearing only in the user goal, "
                "navigation/filter text, or finish message does not change application state. "
                f"Carry「{value}」verbatim into the mutation/data-flow node that sets or creates it."
            ),
            evidence=(f"mention={entity.mention}", f"missing={value}"),
        ))
    return issues


def _value_consumer_text(program: Program) -> str:
    parts: list[str] = []

    def _collect(stmts: list[Stmt]) -> None:
        for stmt in stmts:
            if isinstance(stmt, RunLike) and stmt.kind == "action":
                parts.extend([stmt.name or "", stmt.success_condition or "", stmt.read_spec or ""])
                for value in getattr(stmt, "target_values", {}).values():
                    parts.extend(target_value_options(value))
            elif isinstance(stmt, Compute):
                parts.append(stmt.expr or "")
            elif isinstance(stmt, Call):
                parts.extend(str(value) for value in (stmt.args or {}).values())
            elif isinstance(stmt, ForEach):
                parts.append(stmt.body_goal or "")
                _collect(stmt.body)
            elif isinstance(stmt, If):
                _collect(stmt.then)
                _collect(stmt.otherwise)

    _collect(program.statements)
    for fn in getattr(program, "functions", None) or []:
        _collect(fn.body)
    return "\n".join(parts)


def _check_router_entity_coverage(program: Program, resolution: IntentResolution) -> list[IntentContractIssue]:
    program_text = _program_text(program)
    retrieval_text = _retrieval_text(program)
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
        selector = str(getattr(entity, "selector", "") or "").strip()

        mention_present = _contains(program_text, mention)
        mention_present_in_retrieval = _contains(retrieval_text, mention)
        key_present = _contains(program_text, search_key)
        key_present_as_own_strategy = key_present
        if mention and search_key and _norm(mention) != _norm(search_key):
            retrieval_wo_mention = _remove_norm_phrase(retrieval_text, mention)
            key_present_as_own_strategy = (
                _contains(retrieval_wo_mention, search_key)
                # Relaxed anchor: any substantive token of the mention as the fallback key also
                # satisfies the contract — knowledge may pick a better-discriminating token than
                # the router's choice; the invariant is fallback-anchored-to-mention.
                or _mention_token_fallback_present(retrieval_wo_mention, mention)
            )
        set_scope_decomposed = (
            cardinality == "set"
            and bool(selector)
            and (not search_key or _contains(retrieval_text, search_key))
            and _contains(retrieval_text, selector)
        )

        if mention or search_key:
            if match_mode == "approximate":
                if (
                    mention
                    and search_key
                    and _norm(mention) != _norm(search_key)
                    and not mention_present_in_retrieval
                    and not set_scope_decomposed
                ):
                    issues.append(IntentContractIssue(
                        code="ROUTER_APPROXIMATE_MENTION_DROPPED",
                        message=(
                            f"Router marked entity「{mention}」as approximate with search_key"
                            f"「{search_key}」, but the retrieval/filter/navigation steps do not "
                            f"include the original mention「{mention}」. Approximate lookup must first "
                            f"try the full original value「{mention}」as the exact trial, read a "
                            "match_count/result count, and only in an explicit if count == '0' "
                            f"fallback to search_key「{search_key}」. A K-only first search is invalid "
                            "even when the search_key is a token inside the original mention."
                        ),
                        evidence=(f"mention={mention}", f"search_key={search_key}"),
                    ))
                if search_key and not key_present_as_own_strategy:
                    issues.append(IntentContractIssue(
                        code="ROUTER_APPROXIMATE_KEY_DROPPED",
                        message=(
                            f"Router marked entity「{mention}」as approximate with search_key"
                            f"「{search_key}」, but after removing the original mention from retrieval "
                            f"steps the program does not include search_key「{search_key}」(or another "
                            f"substantive token of「{mention}」) as its own fallback/search-scope "
                            "strategy. Keep the exact trial with the full mention, and add an explicit "
                            "fallback retrieval step using the search_key — or, when app knowledge "
                            "identifies a better-discriminating token of the same mention, that token."
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

        # Aggregate escape: a set may be realized WITHOUT iteration when a single interactive step
        # structurally declares it covers the whole set (covers_set=<mention>) — parent record whose
        # save cascades to members, select-all + mass action, bulk edit. The declaration comes from
        # app knowledge via the decomposer; the contract only checks the declaration exists and the
        # entity scope still appears in retrieval (the aggregate must act on the right group).
        set_covered_by_aggregate = (
            cardinality == "set"
            and _set_covered_by_aggregate(program, mention)
            and (mention_present_in_retrieval or _contains(retrieval_text, search_key) or _contains(retrieval_text, selector))
        )

        if cardinality == "set" and not foreach_stmts and not set_covered_by_aggregate:
            issues.append(IntentContractIssue(
                code="ROUTER_SET_ENTITY_WITHOUT_FOREACH",
                message=(
                    "Router marked an entity as a set, but the program has no foreach iteration. "
                    "Either iterate the matched members (foreach), or — ONLY when app knowledge "
                    "states a single aggregate object/bulk mechanism covers all members at once — "
                    "declare covers_set=<entity mention> on that single mutation step."
                ),
                evidence=(f"mention={mention}", f"selector={getattr(entity, 'selector', '') or ''}"),
            ))
        elif cardinality == "set" and selector and not set_covered_by_aggregate:
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


def _retrieval_text(program: Program) -> str:
    parts: list[str] = []

    def _collect(stmts: list[Stmt]) -> None:
        for stmt in stmts:
            if isinstance(stmt, RunLike) and stmt.kind in {"filter", "navigation"}:
                parts.extend([stmt.name or "", stmt.success_condition or "", getattr(stmt, "read_spec", "") or ""])
            elif isinstance(stmt, ForEach):
                parts.extend([stmt.target or "", stmt.member_desc or "", stmt.body_goal or ""])
                _collect(stmt.body)
            elif isinstance(stmt, If):
                _collect(stmt.then)
                _collect(stmt.otherwise)

    _collect(program.statements)
    for fn in (getattr(program, "functions", None) or []):
        _collect(fn.body)
    return "\n".join(parts)


def _contains(haystack: str, needle: str) -> bool:
    if not needle.strip():
        return False
    return _norm(needle) in _norm(haystack)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def _remove_norm_phrase(text: str, phrase: str) -> str:
    return _norm(text).replace(_norm(phrase), " ")


def _set_covered_by_aggregate(program: Program, mention: str) -> bool:
    """True when some interactive step declares covers_set for this entity (normalized match)."""
    target = _norm(mention)
    if not target:
        return False

    def _scan(stmts: list[Stmt]) -> bool:
        for stmt in stmts:
            declared = _norm(getattr(stmt, "covers_set", "") or "")
            if declared and (declared == target or declared in target or target in declared):
                return True
            if isinstance(stmt, ForEach) and _scan(stmt.body):
                return True
            if isinstance(stmt, If) and (_scan(stmt.then) or _scan(stmt.otherwise)):
                return True
        return False

    if _scan(program.statements):
        return True
    return any(_scan(fn.body) for fn in (getattr(program, "functions", None) or []))


def _mention_token_fallback_present(retrieval_wo_mention: str, mention: str) -> bool:
    """A fallback anchored to ANY substantive token of the original mention counts as a
    fallback strategy: the router's search_key is one valid anchor, but app knowledge may know a
    better discriminating token of the same mention (e.g. the product-line word instead of a
    fabric brand shared across families). The invariant preserved is anchoring-to-the-mention,
    not which token the router happened to pick. Only NAME-LIKE tokens qualify (contains an
    uppercase letter, or CJK): a generic lowercase word ("orders") appearing anywhere in retrieval
    prose must not silence the contract."""
    for token in re.findall(r"[A-Za-z0-9]{3,}|[一-鿿]{2,}", mention or ""):
        if not any(ch.isupper() for ch in token) and token.isascii():
            continue
        if _contains(retrieval_wo_mention, token):
            return True
    return False


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
