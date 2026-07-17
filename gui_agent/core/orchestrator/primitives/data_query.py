"""Restricted in-memory SQL kernel used by the semantic Data executor.

It consumes runtime records/table snapshots and never drives the platform or
writes persistent state. SQL is an executor-private implementation detail, not
a public Program node.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

from ..sql_utils import sql_identifier as _identifier


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
    becomes `item_name`. Display-value columns that parse consistently also expose
    typed shadows: `<column>_num` for numeric/currency/percent text and
    `<column>_ts` for date/time text.
    """
    snapshots = [t for t in (tables or []) if isinstance(t, dict) and t.get("rows")]
    if not snapshots:
        raise DataQueryError("当前观察没有可查询的结构化表格数据")
    normalized_sql = _validate_select_sql(sql)
    normalized_sql = rewrite_quoted_display_identifiers(normalized_sql, snapshots)
    if require_complete:
        referenced = _referenced_snapshot_indexes(normalized_sql, snapshots)
        partial = [
            str(t.get("caption") or t.get("path") or f"table_{i + 1}")
            for i, t in enumerate(snapshots)
            if t.get("partial") and (not referenced or i in referenced)
        ]
        if partial:
            raise DataQueryError(
                "表格快照不完整，不能直接分析；请先分页/采集完整数据。partial tables: "
                + ", ".join(partial[:3])
            )
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
    if re.search(r"\{[^{}]+\}", text):
        raise DataQueryError(
            "data_query SQL 不支持模板表达式 {...}；"
            "SQL 只能查询当前结构化表格或 foreach into 表，差值/比例/合计请在 SQL/CTE 内基于表列计算。"
        )
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
    _reject_top_level_aggregate_limit(text)
    return text


_AGGREGATE_FN_RE = re.compile(r"\b(?:sum|avg|min|max|count)\s*\(", flags=re.IGNORECASE)


def _reject_top_level_aggregate_limit(sql: str) -> None:
    """Reject `SELECT SUM(x) FROM table LIMIT N`.

    In SQL, LIMIT is applied after aggregation. For "sum the last N rows" the
    LIMIT must live inside a subquery that orders/selects those N input rows.
    """
    words = list(_sql_words_with_depth(sql))
    for idx, (select_pos, word, depth) in enumerate(words):
        if word != "select":
            continue
        from_pos = None
        limit_pos = None
        group_pos = None
        for pos, next_word, next_depth in words[idx + 1:]:
            if next_depth < depth:
                break
            if next_depth != depth:
                continue
            if next_word == "select":
                break
            if next_word == "from" and from_pos is None:
                from_pos = pos
            elif next_word == "group" and from_pos is not None and group_pos is None:
                group_pos = pos
            elif next_word == "limit" and from_pos is not None:
                limit_pos = pos
                break
        if from_pos is None or limit_pos is None or group_pos is not None:
            continue
        if _AGGREGATE_FN_RE.search(sql[select_pos:from_pos]):
            raise DataQueryError(
                "SQL 把 LIMIT 放在聚合之后，不能限制 SUM/AVG/COUNT 的输入行；"
                "请先在子查询中 ORDER BY/LIMIT 选出目标行，再在外层聚合，"
                "例如 SELECT SUM(amount_num) AS total FROM "
                "(SELECT amount_num FROM data ORDER BY date_ts DESC LIMIT N)"
            )


def _top_level_keyword_pos(sql: str, keyword: str, *, start: int = 0) -> int | None:
    target = keyword.lower()
    for pos, word in _top_level_words(sql):
        if pos >= start and word == target:
            return pos
    return None


def _top_level_words(sql: str):
    for pos, word, depth in _sql_words_with_depth(sql):
        if depth == 0:
            yield pos, word


def _sql_words_with_depth(sql: str):
    depth = 0
    quote: str | None = None
    i = 0
    while i < len(sql):
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < len(sql) else ""
        if quote:
            if quote == "]":
                if ch == "]":
                    quote = None
            elif ch == quote:
                # SQL escapes quote characters by doubling them.
                if nxt == quote:
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch in {"'", '"', "`"}:
            quote = ch
            i += 1
            continue
        if ch == "[":
            quote = "]"
            i += 1
            continue
        if ch == "-" and nxt == "-":
            end = sql.find("\n", i + 2)
            i = len(sql) if end == -1 else end + 1
            continue
        if ch == "/" and nxt == "*":
            end = sql.find("*/", i + 2)
            i = len(sql) if end == -1 else end + 2
            continue
        if ch == "(":
            depth += 1
            i += 1
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            i += 1
            continue
        if depth == 0 and (ch.isalpha() or ch == "_"):
            j = i + 1
            while j < len(sql) and (sql[j].isalnum() or sql[j] == "_"):
                j += 1
            yield i, sql[i:j].lower(), depth
            i = j
            continue
        if depth != 0 and (ch.isalpha() or ch == "_"):
            j = i + 1
            while j < len(sql) and (sql[j].isalnum() or sql[j] == "_"):
                j += 1
            yield i, sql[i:j].lower(), depth
            i = j
            continue
        i += 1


def _load_tables(conn: sqlite3.Connection, tables: list[dict[str, Any]]) -> None:
    used_aliases: set[str] = set()
    for idx, table in enumerate(tables, 1):
        rows = table.get("rows") or []
        headers = table.get("headers") or _headers_from_rows(rows)
        columns, row_values = _prepare_table_values(headers, rows)
        table_name = f"table_{idx}"
        _create_and_insert(conn, table_name, columns, row_values)
        used_aliases.add(table_name)
        if idx == 1:
            conn.execute(f"CREATE VIEW data AS SELECT * FROM {_quote_ident(table_name)}")
            used_aliases.add("data")
        alias = _identifier(table.get("caption") or "")
        if alias and alias not in used_aliases:
            conn.execute(f"CREATE VIEW {_quote_ident(alias)} AS SELECT * FROM {_quote_ident(table_name)}")
            used_aliases.add(alias)


_TABLE_REF_RE = re.compile(
    r"\b(?:from|join)\s+(?:\"([^\"]+)\"|`([^`]+)`|\[([^\]]+)\]|([A-Za-z_][A-Za-z0-9_]*))",
    flags=re.IGNORECASE,
)


def _referenced_snapshot_indexes(sql: str, tables: list[dict[str, Any]]) -> set[int]:
    """Return snapshot indexes referenced by table aliases in the query.

    Complete foreach materializations and current-page DOM snapshots are often passed
    together. A partial DOM sibling must not block a query that references only the
    complete materialized table, while queries against `data`/caption/table_N still
    need the normal completeness guard.
    """
    aliases = _table_alias_indexes(tables)
    refs: set[int] = set()
    for match in _TABLE_REF_RE.finditer(sql or ""):
        raw = next((g for g in match.groups() if g), "")
        key = _identifier(raw)
        if key in aliases:
            refs.add(aliases[key])
    return refs


def _table_alias_indexes(tables: list[dict[str, Any]]) -> dict[str, int]:
    aliases: dict[str, int] = {}
    used_aliases: set[str] = set()
    for idx, table in enumerate(tables):
        table_name = f"table_{idx + 1}"
        aliases[table_name] = idx
        used_aliases.add(table_name)
        if idx == 0:
            aliases["data"] = idx
            used_aliases.add("data")

        alias = _identifier(table.get("caption") or "")
        if alias and alias not in used_aliases:
            aliases[alias] = idx
            used_aliases.add(alias)
    return aliases


def rewrite_quoted_display_identifiers(sql: str, tables: list[dict[str, Any]]) -> str:
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
        base_columns = _unique_identifiers(headers)
        columns, _ = _prepare_table_values(headers, rows)
        for column in columns:
            add(column, column)
        for header, column in zip(headers, base_columns):
            add(header, column)
            normalized = _identifier(header)
            if normalized:
                add(normalized, column)
            for suffix in ("num", "ts"):
                shadow = f"{column}_{suffix}"
                if shadow in columns:
                    add(f"{header} {suffix}", shadow)
                    add(f"{header}_{suffix}", shadow)

    return mapping


def _prepare_table_values(headers: list[Any], rows: list[Any]) -> tuple[list[str], list[list[str]]]:
    base_columns = _unique_identifiers(headers)
    base_values = [_row_values(row, base_columns) for row in rows]
    shadows = _typed_shadow_columns(base_columns, base_values)
    columns = base_columns + [shadow for _, shadow, _ in shadows]
    prepared_rows: list[list[str]] = []
    for values in base_values:
        extras: list[str] = []
        for source_idx, _shadow, kind in shadows:
            raw = values[source_idx] if source_idx < len(values) else ""
            parsed = _parse_numeric_value(raw) if kind == "num" else _parse_datetime_value(raw)
            extras.append(parsed or "")
        prepared_rows.append(values + extras)
    return columns, prepared_rows


def _typed_shadow_columns(base_columns: list[str], rows: list[list[str]]) -> list[tuple[int, str, str]]:
    shadows: list[tuple[int, str, str]] = []
    used = set(base_columns)
    for idx, column in enumerate(base_columns):
        values = [row[idx] for row in rows if idx < len(row)]
        nonempty = [value for value in values if str(value or "").strip()]
        if not nonempty:
            continue
        date_count = sum(1 for value in nonempty if _parse_datetime_value(value) is not None)
        numeric_count = sum(1 for value in nonempty if _parse_numeric_value(value) is not None)
        threshold = _typed_parse_threshold(len(nonempty))
        if date_count >= threshold:
            shadow = _unique_shadow_identifier(column, "ts", used)
            shadows.append((idx, shadow, "ts"))
            used.add(shadow)
        if numeric_count >= threshold:
            shadow = _unique_shadow_identifier(column, "num", used)
            shadows.append((idx, shadow, "num"))
            used.add(shadow)
    return shadows


def _typed_parse_threshold(nonempty_count: int) -> int:
    return max(1, (nonempty_count * 4 + 4) // 5)


def _unique_shadow_identifier(column: str, suffix: str, used: set[str]) -> str:
    candidate = f"{column}_{suffix}"
    if candidate not in used:
        return candidate
    n = 2
    while f"{candidate}_{n}" in used:
        n += 1
    return f"{candidate}_{n}"


_CURRENCY_SYMBOLS_RE = r"\$€£¥₹₩₪₫฿₽₺₦₱"
_MONTH_RE = re.compile(
    r"\b(?:jan|january|feb|february|mar|march|apr|april|may|jun|june|jul|july|"
    r"aug|august|sep|sept|september|oct|october|nov|november|dec|december)\b",
    flags=re.IGNORECASE,
)


def _parse_numeric_value(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text or _looks_datetime_like(text):
        return None
    text = re.sub(r"\s+", " ", text).replace("−", "-")
    if re.search(r"[A-Za-z]", text):
        return None

    had_currency = bool(re.search(f"[{_CURRENCY_SYMBOLS_RE}]", text))
    negative = False
    if re.fullmatch(r"\([^()]+\)", text):
        negative = True
        text = text[1:-1].strip()
    if text.startswith(("-", "+")):
        negative = text[0] == "-" if not negative else negative
        text = text[1:].strip()

    text = re.sub(f"^[{_CURRENCY_SYMBOLS_RE}]+", "", text).strip()
    text = re.sub(f"[{_CURRENCY_SYMBOLS_RE}]+$", "", text).strip()
    percent = text.endswith("%")
    if percent:
        text = text[:-1].strip()

    had_comma = "," in text
    had_decimal = "." in text
    number_text = text.replace(",", "")
    if not re.fullmatch(r"(?:\d+(?:\.\d+)?|\.\d+)", number_text):
        return None
    if (
        len(number_text) > 1
        and number_text.startswith("0")
        and not (had_currency or had_comma or had_decimal or percent or negative)
    ):
        return None

    number = float(number_text)
    if negative:
        number = -number
    return format(number, ".15g")


_DATETIME_FORMATS = (
    "%b %d, %Y %I:%M:%S %p",
    "%B %d, %Y %I:%M:%S %p",
    "%b %d, %Y %I:%M %p",
    "%B %d, %Y %I:%M %p",
    "%b %d, %Y",
    "%B %d, %Y",
    "%m/%d/%Y %I:%M:%S %p",
    "%m/%d/%Y %I:%M %p",
    "%m/%d/%Y",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
)


def _parse_datetime_value(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text or not _looks_datetime_like(text):
        return None
    text = re.sub(r"\s+", " ", text)
    normalized = re.sub(r"\bSept\b", "Sep", text, flags=re.IGNORECASE)
    iso_text = normalized.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso_text)
    except ValueError:
        dt = None
    if dt is None:
        for fmt in _DATETIME_FORMATS:
            try:
                dt = datetime.strptime(normalized, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return str(int(dt.timestamp()))


def _looks_datetime_like(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    return bool(
        _MONTH_RE.search(value)
        or re.search(r"\d{1,4}[/-]\d{1,2}[/-]\d{1,4}", value)
        or re.search(r"\d{4}-\d{1,2}-\d{1,2}", value)
        or re.search(r"\d{1,2}:\d{2}", value)
    )


def _create_and_insert(conn: sqlite3.Connection, table_name: str, columns: list[str], row_values: list[list[str]]) -> None:
    col_defs = ", ".join(f"{_quote_ident(c)} TEXT" for c in columns)
    conn.execute(f"CREATE TABLE {_quote_ident(table_name)} ({col_defs})")
    if not row_values:
        return
    placeholders = ", ".join("?" for _ in columns)
    sql = f"INSERT INTO {_quote_ident(table_name)} VALUES ({placeholders})"
    conn.executemany(sql, row_values)


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
    result_rows = [{col: row[col] for col in columns} for row in rows]
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
    missing = [
        field for field in returns
        if field.lower() not in {"result", "结果"} and _return_column(field, columns) is None
    ]
    if missing:
        raise DataQueryError(
            "SQL 查询结果缺少 returns 字段: "
            + ", ".join(missing)
            + "；请把 SELECT 输出列 alias 成这些字段，或用单个 result 返回完整结果。"
            + f" 当前输出列: {', '.join(columns) if columns else '(none)'}"
        )
    for field in returns:
        col = _return_column(field, columns)
        if col is not None:
            values = [_stringify(r.get(col, "")) for r in result_rows]
            reads[field] = values[0] if len(values) == 1 else json.dumps(values, ensure_ascii=False)
        elif field.lower() in {"result", "结果"}:
            reads[field] = text_payload
    return reads


def _return_column(field: str, columns: list[str]) -> str | None:
    if field in columns:
        return field
    key = _identifier(field)
    return key if key in columns else None


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
