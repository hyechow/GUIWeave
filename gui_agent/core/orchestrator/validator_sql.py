"""SQL-focused helpers for orchestrator program validation."""

from __future__ import annotations

import re

from .program import Run

SQL_NON_FIELD_TOKENS = {
    "abs", "all", "and", "as", "asc", "avg", "between", "by", "case", "cast", "count", "dense_rank",
    "coalesce", "desc", "distinct", "else", "end", "from", "group", "having", "in", "integer", "is",
    "like", "limit", "max", "min", "not", "null", "offset", "on", "or", "order", "over", "partition",
    "real", "select", "str", "strftime", "sum", "text", "then", "where", "when", "with",
    "data", "result",
    # SQL scalar/string/numeric/window FUNCTIONS — never grid columns. Cleaning a collected cell
    # (e.g. strip "SKU: ..." off a Product name, cast "$45.00"→number) naturally uses these; without
    # the allowlist they were mis-flagged as foreach-returns columns (FOREACH_DQ_GRID_FIELD_MISSING).
    "replace", "trim", "ltrim", "rtrim", "substr", "substring", "instr", "length", "lower", "upper",
    "split_part", "nullif", "ifnull", "round", "rank", "row_number", "lead", "lag", "ntile",
}

_AGGREGATE_FN_RE = re.compile(r"\b(?:sum|avg|min|max|count)\s*\(", flags=re.IGNORECASE)


def data_query_field_tokens(run: Run) -> set[str]:
    """Best-effort field names a data_query appears to consume or return."""
    tokens = {str(item).strip().lower() for item in (run.returns or []) if str(item).strip()}
    tokens.discard("result")
    # Strip single-quoted string literals first: their contents are VALUES (a LIKE pattern like
    # '%Olivia%', or status = 'Complete'), not column identifiers — tokenizing through them would
    # mis-flag the value text as a missing/unknown column. (Double quotes are SQLite identifiers,
    # left intact so real column refs inside them are still validated.)
    sql = re.sub(r"'[^']*'", " ", getattr(run, "sql", "") or "")
    ignored = sql_derived_identifier_tokens(sql)
    for raw in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", sql):
        token = raw.lower()
        if token in ignored or token in SQL_NON_FIELD_TOKENS or re.fullmatch(r"table_\d+", token):
            continue
        tokens.add(token)
    return tokens


def sql_derived_identifier_tokens(sql: str) -> set[str]:
    tokens = {raw.lower() for raw in re.findall(r"\bas\s+([A-Za-z_][A-Za-z0-9_]*)\b", sql or "", flags=re.I)}
    tokens.update(sql_cte_names(sql))
    tokens.update(sql_table_alias_tokens(sql))
    return tokens


def sql_cte_names(sql: str) -> set[str]:
    text = sql or ""
    if not re.match(r"^\s*with\b", text, flags=re.I):
        return set()
    return {
        raw.lower()
        for raw in re.findall(r"(?:\bwith\b|,)\s*([A-Za-z_][A-Za-z0-9_]*)\s+as\s*\(", text, flags=re.I)
    }


def sql_table_alias_tokens(sql: str) -> set[str]:
    """Best-effort aliases for derived tables, e.g. `FROM (...) c, (...) comp`."""
    aliases = {
        raw.lower()
        for raw in re.findall(r"\)\s+(?:as\s+)?([A-Za-z_][A-Za-z0-9_]*)\b", sql or "", flags=re.I)
    }
    return {alias for alias in aliases if alias not in SQL_NON_FIELD_TOKENS}


def sql_referenced_tables(sql: str) -> set[str]:
    return {
        raw.lower()
        for raw in re.findall(r"\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_]*)\b", sql or "", flags=re.I)
    }


def missing_query_fields(needed: set[str], available: set[str], ignored: set[str] | None = None) -> set[str]:
    ignored = ignored or set()
    return {
        token for token in needed
        if token not in ignored and not query_field_available(token, available)
    }


def query_field_available(token: str, available: set[str]) -> bool:
    if token in available:
        return True
    for suffix in ("_num", "_ts"):
        if token.endswith(suffix) and token[: -len(suffix)] in available:
            return True
    return False


def rank_query_drops_ties(goal_text: str, run: Run) -> bool:
    haystack = f"{goal_text}\n{run.name}\n{run.success_condition}".lower()
    if not re.search(r"\b(second|third|fourth|fifth|rank|most|least)\b|第[二三四五]|最多|最少|排名|并列", haystack):
        return False
    sql = (getattr(run, "sql", "") or "").lower()
    if not re.search(r"\b(count|group\s+by)\b", sql):
        return False
    return bool(re.search(r"\blimit\s+1\b", sql) or re.search(r"\boffset\s+\d+\b", sql))


def aggregate_query_limits_after_aggregation(sql: str) -> bool:
    text = sql or ""
    words = list(sql_words_with_depth(text))
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
        if from_pos is not None and limit_pos is not None and group_pos is None:
            if _AGGREGATE_FN_RE.search(text[select_pos:from_pos]):
                return True
    return False


def temporal_limit_without_order(goal_text: str, run: Run) -> bool:
    haystack = f"{goal_text}\n{run.name}\n{run.success_condition}".lower()
    if not re.search(r"\b(last|recent|latest|newest|oldest)\b|最近|最后|最新|最旧|最早", haystack):
        return False
    return sql_has_limit_without_same_level_order(getattr(run, "sql", "") or "")


def temporal_aggregate_without_row_limit(goal_text: str, run: Run) -> bool:
    haystack = f"{goal_text}\n{run.name}\n{run.success_condition}".lower()
    if not re.search(r"\b(last|recent|latest|newest|oldest)\b|最近|最后|最新|最旧|最早", haystack):
        return False
    sql = (getattr(run, "sql", "") or "").lower()
    if not _AGGREGATE_FN_RE.search(sql):
        return False
    if re.search(r"\blimit\s+\d+\b", sql):
        return False
    if re.search(r"\b(row_number|rank|dense_rank)\s*\(", sql):
        return False
    return True


def sql_has_limit_without_same_level_order(sql: str) -> bool:
    words = list(sql_words_with_depth(sql or ""))
    for idx, (select_pos, word, depth) in enumerate(words):
        if word != "select":
            continue
        limit_pos = None
        order_pos = None
        for pos, next_word, next_depth in words[idx + 1:]:
            if next_depth < depth:
                break
            if next_depth != depth:
                continue
            if next_word == "select":
                break
            if next_word == "order" and order_pos is None:
                order_pos = pos
            elif next_word == "limit":
                limit_pos = pos
                break
        if limit_pos is not None and order_pos is None:
            return True
    return False


def top_level_sql_keyword_pos(sql: str, keyword: str, *, start: int = 0) -> int | None:
    target = keyword.lower()
    for pos, word in top_level_sql_words(sql):
        if pos >= start and word == target:
            return pos
    return None


def top_level_sql_words(sql: str):
    for pos, word, depth in sql_words_with_depth(sql):
        if depth == 0:
            yield pos, word


def sql_words_with_depth(sql: str):
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
        if ch.isalpha() or ch == "_":
            j = i + 1
            while j < len(sql) and (sql[j].isalnum() or sql[j] == "_"):
                j += 1
            yield i, sql[i:j].lower(), depth
            i = j
            continue
        i += 1


def sql_uses_schema_mapping_text(sql: str) -> bool:
    """Reject copied schema display forms such as `Header->column` in SQL."""
    return bool(re.search(r"\b[a-zA-Z_][\w .\"'`-]*\s*->\s*[a-zA-Z_]\w*\b", sql or ""))


def sql_contains_template_ref(sql: str) -> bool:
    """SQL is not a template surface; `{var[field]}` belongs in run text/finish only."""
    return bool(re.search(r"\{[^{}]+\}", sql or ""))


def sql_uses_quoted_display_identifier(sql: str) -> bool:
    """Reject quoted UI labels such as `"Item Name"`; data_query columns are snake_case."""
    for pattern in (r'"([^"]+)"', r"`([^`]+)`", r"\[([^\]]+)\]"):
        for raw in re.findall(pattern, sql or ""):
            text = str(raw or "").strip()
            if text and sql_identifier(text) != text:
                return True
    return False


def sql_identifier(value: object) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^0-9a-zA-Z]+", "_", text).strip("_")
    if not text:
        return ""
    if text[0].isdigit():
        text = "c_" + text
    return text
