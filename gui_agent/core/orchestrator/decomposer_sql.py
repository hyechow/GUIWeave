"""SQL normalization passes used by the orchestrator decomposer."""

from __future__ import annotations

import re

from .data_query import rewrite_quoted_display_identifiers
from .program import Call, Compute, ForEach, If, Program, RunLike, Stmt
from .sql_utils import sql_identifier


def _normalize_data_query_display_identifiers(program: Program) -> Program:
    """Rewrite quoted UI display labels in SQL to known normalized table identifiers.

    Runtime data_query already maps structured-table display headers to normalized SQL
    identifiers. Do the same deterministic rewrite at compile time for tables that the
    program itself declares via foreach row_fields/returns/output_fields, so validation feedback
    does not get stuck on harmless display-label quoting.
    """
    updated = program.model_copy(deep=True)

    def _field_headers_from_body(stmts: list[Stmt]) -> list[str]:
        headers: list[str] = []
        for item in stmts:
            if isinstance(item, RunLike):
                headers.extend(item.returns or [])
            elif isinstance(item, Compute):
                headers.append(item.var)
            elif isinstance(item, If):
                headers.extend(_field_headers_from_body(item.then))
                headers.extend(_field_headers_from_body(item.otherwise))
            elif isinstance(item, ForEach):
                headers.extend(item.row_fields or item.returns or [])
                headers.extend(item.output_fields or [])
        return _unique_headers(headers)

    def _unique_headers(headers: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for header in headers:
            text = str(header or "").strip()
            key = sql_identifier(text)
            if not text or key in seen:
                continue
            out.append(text)
            seen.add(key)
        return out

    def _walk(stmts: list[Stmt], tables: list[dict]) -> None:
        local_tables = list(tables)
        for stmt in stmts:
            if isinstance(stmt, RunLike) and stmt.kind == "data_query" and stmt.sql:
                stmt.sql = rewrite_quoted_display_identifiers(stmt.sql, local_tables)
            elif isinstance(stmt, ForEach):
                headers = _unique_headers([
                    *(stmt.row_fields or stmt.returns or []),
                    *(stmt.output_fields or []),
                    *_field_headers_from_body(stmt.body),
                ])
                caption = stmt.into or f"{stmt.var}s"
                if caption and headers:
                    local_tables.append({"caption": caption, "headers": headers, "rows": []})
                _walk(stmt.body, list(local_tables))
            elif isinstance(stmt, If):
                _walk(stmt.then, list(local_tables))
                _walk(stmt.otherwise, list(local_tables))
            elif isinstance(stmt, Call):
                continue

    _walk(updated.statements, [])
    return updated


def _iter_runs(stmts: list[Stmt]):
    for stmt in stmts:
        if isinstance(stmt, RunLike):
            yield stmt
        elif isinstance(stmt, If):
            yield from _iter_runs(stmt.then)
            yield from _iter_runs(stmt.otherwise)
        elif isinstance(stmt, ForEach):
            yield from _iter_runs(stmt.body)


def _normalize_approximate_entity_sql(program: Program, resolution: object | None) -> Program:
    """Use intent search keys, not approximate spoken mentions, inside SQL filters."""
    if resolution is None or not getattr(resolution, "entities", None):
        return program
    replacements: list[tuple[str, str]] = []
    for entity in resolution.entities:
        if entity.match_mode != "approximate":
            continue
        mention = (entity.mention or "").strip()
        key = (entity.search_key or "").strip()
        if not mention or not key or _norm_sql_text(mention) == _norm_sql_text(key):
            continue
        replacements.append((mention, key.replace("'", "''")))
    if not replacements:
        return program
    updated = program.model_copy(deep=True)
    for run in _iter_runs(updated.statements):
        if run.kind != "data_query" or not run.sql:
            continue
        sql = run.sql
        for mention, key in replacements:
            sql = re.sub(re.escape(mention), key, sql, flags=re.IGNORECASE)
        run.sql = sql
    return updated


def _norm_sql_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()
