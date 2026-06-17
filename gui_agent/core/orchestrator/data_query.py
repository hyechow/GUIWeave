"""Restricted in-memory SQL over structured table snapshots.

`data_query` is a non-UI primitive: it consumes rows already collected by the
perception layer and returns structured values to the orchestrator. It never
drives the browser and never writes to disk.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from typing import Any


class DataQueryError(ValueError):
    """A data query could not be safely executed."""


def execute_data_query(
    tables: list[dict[str, Any]] | None,
    sql: str,
    returns: list[str] | None = None,
    *,
    max_rows: int = 200,
    timeout_s: float = 1.0,
    require_complete: bool = True,
) -> dict[str, str]:
    """Run a restricted read-only SQL query over structured table snapshots.

    The first table is available as `data`; every table is also available as
    `table_1`, `table_2`, ... and, when possible, a sanitized caption alias.
    Column names are normalized to snake_case identifiers, e.g. "Item Name"
    becomes `item_name`.
    """
    snapshots = [t for t in (tables or []) if isinstance(t, dict) and t.get("rows")]
    if not snapshots:
        raise DataQueryError("当前观察没有可查询的结构化表格数据")
    if require_complete:
        partial = [str(t.get("caption") or t.get("path") or f"table_{i}") for i, t in enumerate(snapshots, 1) if t.get("partial")]
        if partial:
            raise DataQueryError(
                "表格快照不完整，不能直接分析；请先分页/采集完整数据。partial tables: "
                + ", ".join(partial[:3])
            )

    normalized_sql = _validate_select_sql(sql)
    normalized_sql = _rewrite_quoted_display_identifiers(normalized_sql, snapshots)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        _load_tables(conn, snapshots)
        _install_read_only_authorizer(conn)
        deadline = time.monotonic() + max(timeout_s, 0.05)
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 2000)
        cur = conn.execute(normalized_sql)
        rows = cur.fetchmany(max_rows + 1)
        truncated = len(rows) > max_rows
        rows = rows[:max_rows]
        columns = [d[0] for d in (cur.description or [])]
        return _reads_from_rows(rows, columns, returns or ["result"], truncated=truncated)
    except sqlite3.Error as exc:
        raise DataQueryError(f"SQL 查询失败: {exc}") from exc
    finally:
        conn.close()


def _validate_select_sql(sql: str) -> str:
    text = (sql or "").strip()
    if not text:
        raise DataQueryError("data_query 缺少 SQL")
    if len(text) > 10000:
        raise DataQueryError("SQL 过长")
    if text.endswith(";"):
        text = text[:-1].strip()
    if ";" in text:
        raise DataQueryError("SQL 只允许单条语句")
    first = re.match(r"^\s*([a-zA-Z_]+)", text)
    keyword = (first.group(1).lower() if first else "")
    if keyword not in {"select", "with"}:
        raise DataQueryError("SQL 只允许 SELECT 或 WITH ... SELECT")
    if keyword == "with" and not re.search(r"\bselect\b", text, flags=re.IGNORECASE):
        raise DataQueryError("WITH 查询必须产生 SELECT 结果")
    forbidden = re.search(
        r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|pragma|vacuum|reindex)\b",
        text,
        flags=re.IGNORECASE,
    )
    if forbidden:
        raise DataQueryError(f"SQL 包含禁止关键字: {forbidden.group(1)}")
    return text


def _load_tables(conn: sqlite3.Connection, tables: list[dict[str, Any]]) -> None:
    used_aliases: set[str] = set()
    for idx, table in enumerate(tables, 1):
        rows = table.get("rows") or []
        headers = table.get("headers") or _headers_from_rows(rows)
        columns = _unique_identifiers(headers)
        table_name = f"table_{idx}"
        _create_and_insert(conn, table_name, columns, rows)
        used_aliases.add(table_name)
        if idx == 1:
            conn.execute(f"CREATE VIEW data AS SELECT * FROM {_quote_ident(table_name)}")
            used_aliases.add("data")
        alias = _identifier(table.get("caption") or "")
        if alias and alias not in used_aliases:
            conn.execute(f"CREATE VIEW {_quote_ident(alias)} AS SELECT * FROM {_quote_ident(table_name)}")
            used_aliases.add(alias)


def _rewrite_quoted_display_identifiers(sql: str, tables: list[dict[str, Any]]) -> str:
    """Map quoted source labels to the normalized identifiers exposed to SQLite.

    The planner is expected to write normalized identifiers (`item_name`), but LLMs
    occasionally copy UI labels into SQL (`"Item Name"`). SQLite treats unknown
    double-quoted names leniently in some contexts, which can produce an empty result instead
    of a hard error. Rewriting exact/normalized label matches here is a generic recovery layer
    for any structured table snapshot.
    """
    aliases = _identifier_aliases(tables)
    if not aliases:
        return sql

    def lookup(raw: str) -> str | None:
        text = str(raw or "").strip()
        return aliases.get(text.lower()) or aliases.get(_identifier(text).lower())

    def repl_double(match: re.Match[str]) -> str:
        target = lookup(match.group(1))
        return _quote_ident(target) if target else match.group(0)

    def repl_backtick(match: re.Match[str]) -> str:
        target = lookup(match.group(1))
        return _quote_ident(target) if target else match.group(0)

    def repl_bracket(match: re.Match[str]) -> str:
        target = lookup(match.group(1))
        return _quote_ident(target) if target else match.group(0)

    text = re.sub(r'"([^"]+)"', repl_double, sql)
    text = re.sub(r"`([^`]+)`", repl_backtick, text)
    text = re.sub(r"\[([^\]]+)\]", repl_bracket, text)
    return text


def _identifier_aliases(tables: list[dict[str, Any]]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    ambiguous: set[str] = set()
    used_aliases: set[str] = set()

    def add(raw: Any, target: str) -> None:
        key = str(raw or "").strip().lower()
        if not key:
            return
        if key in ambiguous:
            return
        prev = mapping.get(key)
        if prev is not None and prev != target:
            mapping.pop(key, None)
            ambiguous.add(key)
            return
        mapping[key] = target

    for idx, table in enumerate(tables, 1):
        table_name = f"table_{idx}"
        add(table_name, table_name)
        used_aliases.add(table_name)
        if idx == 1:
            add("data", "data")
            used_aliases.add("data")

        alias = _identifier(table.get("caption") or "")
        if alias and alias not in used_aliases:
            add(alias, alias)
            add(table.get("caption") or "", alias)
            used_aliases.add(alias)

        rows = table.get("rows") or []
        headers = table.get("headers") or _headers_from_rows(rows)
        columns = _unique_identifiers(headers)
        for header, column in zip(headers, columns):
            add(column, column)
            add(header, column)
            normalized = _identifier(header)
            if normalized:
                add(normalized, column)

    return mapping


def _create_and_insert(conn: sqlite3.Connection, table_name: str, columns: list[str], rows: list[Any]) -> None:
    col_defs = ", ".join(f"{_quote_ident(c)} TEXT" for c in columns)
    conn.execute(f"CREATE TABLE {_quote_ident(table_name)} ({col_defs})")
    if not rows:
        return
    placeholders = ", ".join("?" for _ in columns)
    sql = f"INSERT INTO {_quote_ident(table_name)} VALUES ({placeholders})"
    conn.executemany(sql, [_row_values(row, columns) for row in rows])


def _row_values(row: Any, columns: list[str]) -> list[str]:
    if isinstance(row, dict):
        by_norm = {_identifier(k): v for k, v in row.items()}
        return ["" if by_norm.get(c) is None else str(by_norm.get(c)) for c in columns]
    if isinstance(row, list):
        vals = ["" if v is None else str(v) for v in row]
        return vals[: len(columns)] + [""] * max(0, len(columns) - len(vals))
    return [""] * len(columns)


def _headers_from_rows(rows: list[Any]) -> list[str]:
    for row in rows:
        if isinstance(row, dict):
            return list(row.keys())
        if isinstance(row, list):
            return [f"col_{i}" for i in range(1, len(row) + 1)]
    return []


def _unique_identifiers(headers: list[Any]) -> list[str]:
    out: list[str] = []
    seen: dict[str, int] = {}
    for idx, header in enumerate(headers, 1):
        base = _identifier(header) or f"col_{idx}"
        n = seen.get(base, 0) + 1
        seen[base] = n
        out.append(base if n == 1 else f"{base}_{n}")
    return out


def _identifier(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^0-9a-zA-Z]+", "_", text).strip("_")
    if not text:
        return ""
    if text[0].isdigit():
        text = "c_" + text
    return text


def _quote_ident(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _install_read_only_authorizer(conn: sqlite3.Connection) -> None:
    allowed = {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
    }

    def _authorizer(action_code, _arg1, _arg2, _db_name, _trigger_name):
        return sqlite3.SQLITE_OK if action_code in allowed else sqlite3.SQLITE_DENY

    conn.set_authorizer(_authorizer)


def _reads_from_rows(
    rows: list[sqlite3.Row],
    columns: list[str],
    returns: list[str],
    *,
    truncated: bool,
) -> dict[str, str]:
    result_rows = [{col: _stringify(row[col]) for col in columns} for row in rows]
    payload = _compact_payload(result_rows, columns)
    if truncated:
        payload = {"rows": result_rows, "truncated": True}
    text_payload = _json_or_scalar(payload)

    reads: dict[str, str] = {}
    if not returns:
        return {"result": text_payload}
    if len(returns) == 1:
        reads[returns[0]] = text_payload
        return reads
    for field in returns:
        key = _identifier(field)
        if key in columns:
            values = [r.get(key, "") for r in result_rows]
            reads[field] = values[0] if len(values) == 1 else json.dumps(values, ensure_ascii=False)
        elif field.lower() in {"result", "结果"}:
            reads[field] = text_payload
        else:
            reads[field] = ""
    return reads


def _compact_payload(rows: list[dict[str, str]], columns: list[str]) -> Any:
    if len(columns) == 1:
        values = [row.get(columns[0], "") for row in rows]
        if len(values) == 1:
            return values[0]
        return values
    if len(rows) == 1:
        return rows[0]
    return rows


def _json_or_scalar(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _stringify(value: Any) -> str:
    return "" if value is None else str(value)
