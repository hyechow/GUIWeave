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
    issues.extend(_check_multi_value_binding(program, resolution))
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
        if not _is_value_role(entity):
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
                f"Router marked「{value}」as {_entity_role(entity)}, but no action, compute, call argument, or "
                "foreach body_goal consumes that value. A value appearing only in the user goal, "
                "navigation/filter text, or finish message does not change application state. "
                f"Carry「{value}」verbatim into the mutation/data-flow node that sets or creates it."
            ),
            evidence=(f"mention={entity.mention}", f"missing={value}"),
        ))
    return issues


def _value_consumer_text(program: Program) -> str:
    parts: list[str] = []
    for stmt in _program_statements(program):
        if isinstance(stmt, RunLike) and stmt.kind == "action":
            parts.extend((stmt.name, stmt.success_condition, stmt.read_spec))
            for value in stmt.target_values.values():
                parts.extend(target_value_options(value))
        elif isinstance(stmt, Compute):
            parts.append(stmt.expr)
        elif isinstance(stmt, Call):
            parts.extend(str(value) for value in stmt.args.values())
        elif isinstance(stmt, ForEach):
            parts.append(stmt.body_goal)
    return "\n".join(parts)


def _check_multi_value_binding(
    program: Program,
    resolution: IntentResolution,
) -> list[IntentContractIssue]:
    """Keep one logical multi-selection together and do not redefine selection-only values."""
    actions = [
        (
            stmt,
            [
                {_norm(value) for value in target_value_options(raw)}
                for raw in stmt.target_values.values()
            ],
        )
        for stmt in _program_statements(program)
        if isinstance(stmt, RunLike) and stmt.kind == "action"
    ]

    issues: list[IntentContractIssue] = []
    target_members = {
        _norm(value)
        for entity in resolution.entities
        if _entity_role(entity) == "target_value"
        for value in (
            target_value_options(entity.value_members)
            or (str(entity.mention or entity.search_key),)
        )
        if str(value).strip()
    }
    for entity in resolution.entities:
        if not _is_value_role(entity):
            continue
        members = {_norm(value) for value in target_value_options(entity.value_members)}
        if len(members) < 2:
            continue
        grouped = [
            item
            for item in actions
            if any(members <= group for group in item[1])
            and (
                not target_members
                or any(target_members & group for group in item[1])
            )
        ]
        if not grouped:
            issues.append(IntentContractIssue(
                code="ROUTER_MULTI_VALUE_SPLIT",
                message=(
                    f"Router 声明「{entity.mention}」是同一选择组的原子值 {sorted(members)}，"
                    "但没有任一 action 在 target_values 的同一字段中同时承载它们。"
                    "请用一个数组字段保留完整选择集，不要拆成多个独立 mutation。"
                ),
            ))
            continue
        partial = [
            action.name
            for action, groups in actions
            if any(members & group and not members <= group for group in groups)
        ]
        if partial:
            issues.append(IntentContractIssue(
                code="ROUTER_MULTI_VALUE_SPLIT",
                message=(
                    f"Router 声明「{entity.mention}」是同一选择组的原子值 {sorted(members)}，"
                    f"但这些 action 只承载了其中一部分：{partial}。即使另一个 action 已承载完整数组，"
                    "也不能再按成员拆成独立 mutation；每个消费该选择组的 action 都必须在同一字段"
                    "中保留完整数组，或删除多余的成员级 action。"
                ),
            ))
        if _entity_role(entity) != "qualifier_value":
            continue
        grouped_ids = {id(item[0]) for item in grouped}
        extras = [
            action.name
            for action, groups in actions
            if id(action) not in grouped_ids
            and (
                any(members & group for group in groups)
                or any(_contains(_action_text(action), member) for member in members)
            )
        ]
        if extras:
            issues.append(IntentContractIssue(
                code="ROUTER_SELECTION_VALUE_REDEFINED",
                message=(
                    f"Router 声明「{entity.mention}」只是最终选择值，但计划又在其他 action "
                    f"中单独创建/改写了它：{extras}。保留承载完整选择集的最终 mutation；"
                    "必须删除这些 action 及专门为它们准备的 navigation/filter/if 整个前置资源阶段，"
                    "不能只把多个前置合并或改名。该值仍保留在最终 mutation 的 target_values 中。"
                ),
            ))
    return issues


def _action_text(action: RunLike) -> str:
    return "\n".join((action.name or "", action.success_condition or "", action.read_spec or ""))


def _check_router_entity_coverage(program: Program, resolution: IntentResolution) -> list[IntentContractIssue]:
    program_text = _program_text(program)
    retrieval_text = _retrieval_text(program)
    foreach_stmts = _iter_foreaches(program.statements)
    issues: list[IntentContractIssue] = []

    for entity in resolution.entities:
        # Value entities are values to set/select (a new rule name, a form scope) — used verbatim,
        # never searched, never iterated. Coverage checks on them false-block create/update tasks.
        if _is_value_role(entity):
            continue

        collection_scope = _is_collection_scope(entity)
        mention = str(entity.mention or "").strip()
        search_key = str(entity.search_key or "").strip()
        match_mode = str(entity.match_mode or "").strip().lower()
        cardinality = str(getattr(entity, "cardinality", "") or "").strip().lower()
        selector = str(getattr(entity, "selector", "") or "").strip()

        mention_present = _contains(program_text, mention)
        mention_present_in_retrieval = _contains_filter_value(program, mention)
        key_present = _contains(program_text, search_key)
        set_scope_decomposed = (
            cardinality == "set"
            and bool(selector)
            and (not search_key or _contains(retrieval_text, search_key))
            and _contains(retrieval_text, selector)
        )
        key_present_as_own_strategy = set_scope_decomposed or _has_zero_count_fallback(
            program, mention=mention, search_key=search_key
        )

        if (mention or search_key) and not collection_scope:
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
                            f"「{search_key}」, but the exact filter's explicit zero-count branch does "
                            f"not retry the same field with search_key「{search_key}」. Keep the exact "
                            "trial with the full mention, then use the router-provided search_key "
                            "verbatim in that fallback. If the key is wrong, correct intent resolution "
                            "instead of overriding its contract downstream."
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
            and (
                collection_scope
                or mention_present_in_retrieval
                or _contains(retrieval_text, search_key)
                or _contains(retrieval_text, selector)
            )
        )
        set_covered_by_iteration = (
            any(
                _foreach_declares_scope(loop, mention=mention, selector=selector)
                for loop in foreach_stmts
            )
            if collection_scope
            else bool(foreach_stmts)
        )

        if cardinality == "set" and not set_covered_by_iteration and not set_covered_by_aggregate:
            issues.append(IntentContractIssue(
                code="ROUTER_SET_ENTITY_WITHOUT_FOREACH",
                message=(
                    "Router marked an entity as a set, but the program has no matching foreach iteration. "
                    "Either iterate the matched members (foreach), or — ONLY when app knowledge "
                    "states a single aggregate object/bulk mechanism covers all members at once — "
                    "declare covers_set=<entity mention> on that single mutation step. A foreach for "
                    "collecting some other entity does not cover this set; collection_scope must be "
                    "copied into that foreach's member_desc/body_goal or the aggregate action's covers_set."
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

    for stmt in _program_statements(program):
        if isinstance(stmt, ForEach):
            into_tables.add((stmt.into or f"{stmt.var}s").strip().lower())
            ui_text_parts.extend((stmt.target, stmt.member_desc, stmt.body_goal))
        elif isinstance(stmt, Query):
            scoped_queries.append(stmt)
        elif isinstance(stmt, Run):
            ui_text_parts.extend((stmt.name, stmt.success_condition))
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
        if _is_value_role(entity) or _is_collection_scope(entity):
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


def _is_value_role(entity: object) -> bool:
    return _entity_role(entity) in {"target_value", "qualifier_value"}


def _is_collection_scope(entity: object) -> bool:
    return _entity_role(entity) == "collection_scope"


def _program_text(program: Program) -> str:
    return program.model_dump_json(exclude_none=True)


def _retrieval_text(program: Program) -> str:
    parts: list[str] = []
    for stmt in _program_statements(program):
        if isinstance(stmt, RunLike) and stmt.kind in {"filter", "navigation"}:
            parts.extend((stmt.name, stmt.success_condition, stmt.read_spec))
        elif isinstance(stmt, ForEach):
            parts.extend((stmt.target, stmt.member_desc, stmt.body_goal))
    return "\n".join(parts)


def _filter_text(stmt: RunLike) -> str:
    values = " ".join(str(value) for value in stmt.target_values.values())
    return "\n".join((stmt.name, stmt.success_condition, stmt.read_spec, values))


def _contains_filter_value(program: Program, value: str) -> bool:
    return any(
        stmt.kind == "filter" and _contains(_filter_text(stmt), value)
        for stmt in _program_statements(program)
        if isinstance(stmt, RunLike)
    )


def _has_zero_count_fallback(
    program: Program,
    *,
    mention: str,
    search_key: str,
) -> bool:
    if not mention or not search_key or _norm(mention) == _norm(search_key):
        return _contains_filter_value(program, search_key)
    trials = {
        stmt.var
        for stmt in _program_statements(program)
        if isinstance(stmt, RunLike)
        and stmt.kind == "filter"
        and stmt.var
        and "match_count" in stmt.returns
        and _contains(_filter_text(stmt), mention)
    }
    for stmt in _program_statements(program):
        if not isinstance(stmt, If) or stmt.cond.var not in trials:
            continue
        if stmt.cond.field != "match_count" or str(stmt.cond.value) != "0":
            continue
        if stmt.cond.cmp == "==":
            branch = stmt.then
        elif stmt.cond.cmp == "!=":
            branch = stmt.otherwise
        else:
            continue
        for fallback in _walk_statements(branch):
            if not isinstance(fallback, RunLike) or fallback.kind != "filter":
                continue
            text = _remove_norm_phrase(_filter_text(fallback), mention)
            if _contains(text, search_key):
                return True
    return False


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


def _walk_statements(stmts: list[Stmt]):
    for stmt in stmts:
        yield stmt
        if isinstance(stmt, If):
            yield from _walk_statements(stmt.then)
            yield from _walk_statements(stmt.otherwise)
        elif isinstance(stmt, ForEach):
            yield from _walk_statements(stmt.body)


def _program_statements(program: Program):
    yield from _walk_statements(program.statements)
    for function in program.functions:
        yield from _walk_statements(function.body)


def _iter_foreaches(stmts: list[Stmt]) -> list[ForEach]:
    return [stmt for stmt in _walk_statements(stmts) if isinstance(stmt, ForEach)]


def _iter_runs(stmts: list[Stmt]) -> list[RunLike]:
    return [stmt for stmt in _walk_statements(stmts) if isinstance(stmt, RunLike)]


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


def _foreach_declares_scope(
    loop: ForEach,
    *,
    mention: str,
    selector: str,
) -> bool:
    """Whether this loop explicitly owns one Router collection scope."""
    text = " ".join((loop.target, loop.member_desc, loop.body_goal))
    return any(
        _contains(text, value)
        for value in (mention, selector)
        if str(value or "").strip()
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
