"""URL/link capability validation rules for orchestrator programs."""

from __future__ import annotations

import re

from ..program import BARE_REF_RE, TEMPLATE_RE, Call, Compute, ForEach, FunctionDef, If, Query, Run, RunLike, Stmt
from .issue import IssueList

_URL_CAPABILITY_RE = re.compile(r"(?:url|href|link|链接|网址|詳細連結|详情链接)", re.IGNORECASE)
_DETAIL_OPEN_RE = re.compile(
    r"\b(?:open|edit|view|drill\s+into|detail|record)\b|打开|点开|点击|编辑页|详情页|详情|明细|记录页",
    re.IGNORECASE,
)


def template_fields_for_var(text: str, var: str) -> set[str]:
    return {
        match.group(2).strip().strip("'\"")
        for match in TEMPLATE_RE.finditer(text or "")
        if match.group(1) == var
    }


def _is_url_capability(name: str) -> bool:
    return bool(_URL_CAPABILITY_RE.search(str(name or "")))


def _top_level_keyword(text: str, keyword: str, *, start: int = 0) -> tuple[int, int] | None:
    """Locate a SQL keyword outside parentheses and quoted strings."""
    depth = 0
    quote = ""
    index = start
    wanted = keyword.casefold()
    while index < len(text):
        char = text[index]
        if quote:
            if char == quote:
                if index + 1 < len(text) and text[index + 1] == quote:
                    index += 2
                    continue
                quote = ""
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            continue
        if char == "(":
            depth += 1
            index += 1
            continue
        if char == ")":
            depth = max(0, depth - 1)
            index += 1
            continue
        if depth == 0 and text[index:index + len(keyword)].casefold() == wanted:
            before = text[index - 1] if index else " "
            after_index = index + len(keyword)
            after = text[after_index] if after_index < len(text) else " "
            if not (before.isalnum() or before == "_") and not (
                after.isalnum() or after == "_"
            ):
                return index, after_index
        index += 1
    return None


def _split_top_level_csv(text: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    quote = ""
    start = 0
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            if char == quote:
                if index + 1 < len(text) and text[index + 1] == quote:
                    index += 2
                    continue
                quote = ""
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            parts.append(text[start:index].strip())
            start = index + 1
        index += 1
    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def _select_projection(sql: str) -> dict[str, str]:
    select = _top_level_keyword(sql, "select")
    if select is None:
        return {}
    from_kw = _top_level_keyword(sql, "from", start=select[1])
    body = sql[select[1]:from_kw[0] if from_kw else len(sql)]
    projections: dict[str, str] = {}
    for item in _split_top_level_csv(body):
        alias_match = re.search(
            r"\bas\s+([A-Za-z_][A-Za-z0-9_]*)\s*$",
            item,
            flags=re.IGNORECASE,
        )
        if alias_match:
            alias = alias_match.group(1)
            expression = item[:alias_match.start()].strip()
        elif re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", item):
            alias = item
            expression = item
        else:
            continue
        projections[alias.casefold()] = expression
    return projections


def check_query_url_projection(query: Query, issues: IssueList) -> None:
    """Require URL-typed results to originate from a URL/link capability."""
    url_fields = [field for field in query.returns if _is_url_capability(field)]
    if not url_fields:
        return
    projections = _select_projection(query.sql or "")
    for field in url_fields:
        expression = projections.get(str(field).casefold(), "")
        identifiers = re.findall(
            r"\b[A-Za-z_][A-Za-z0-9_]*\b",
            re.sub(r"'[^']*'|\"[^\"]*\"", " ", expression),
        )
        if expression and (
            re.search(r"https?://", expression, flags=re.IGNORECASE)
            or any(_is_url_capability(identifier) for identifier in identifiers)
        ):
            continue
        issues.add(
            "DATA_QUERY_URL_ALIAS_NOT_URL_SOURCE",
            f"data_query 步「{query.name}」把字段「{field}」声明成 URL/link 能力，"
            f"但 SELECT 表达式 {expression or '<missing>'!r} 没有来自任何 URL/HREF/link 列。"
            "显示文本（如 Action='Edit'）不能通过 `AS detail_url` 冒充可导航入口；"
            "请让 foreach 采集真实 *_url/*_href/*_link 字段，并从该字段投影结果。",
            evidence=(field, expression),
        )


def _run_text(run: Run) -> str:
    return f"{run.name}\n{run.success_condition}\n{run.read_spec}"


def _run_looks_like_detail_open(run: Run) -> bool:
    return run.kind in {"navigation", "action", "read"} and bool(_DETAIL_OPEN_RE.search(_run_text(run)))


def _field_key(field: str) -> str:
    return str(field or "").strip().strip("'\"").lower()


def _run_uses_any_url_param(run: Run, url_params: set[str]) -> bool:
    text = _run_text(run)
    return any(f"{{{param}}}" in text for param in url_params)


def _run_references_any_param(run: Run, params: set[str]) -> bool:
    if not params:
        return False
    refs = set(BARE_REF_RE.findall(_run_text(run)))
    return bool(refs & params)


def _function_opens_detail(fn: FunctionDef, function_defs: dict[str, FunctionDef], seen: set[str] | None = None) -> bool:
    seen = seen or set()
    if fn.name in seen:
        return False
    seen.add(fn.name)

    def _walk(seq: list[Stmt]) -> bool:
        for item in seq:
            if isinstance(item, RunLike) and _run_looks_like_detail_open(item):
                return True
            if isinstance(item, If) and (_walk(item.then) or _walk(item.otherwise)):
                return True
            if isinstance(item, ForEach) and _walk(item.body):
                return True
            if isinstance(item, Call):
                child = function_defs.get(item.func)
                if child is not None and _function_opens_detail(child, function_defs, seen):
                    return True
        return False

    return _walk(fn.body)


def check_foreach_url_policy(
    loop: ForEach,
    row_fields: set[str],
    function_defs: dict[str, FunctionDef],
    issues: IssueList,
) -> None:
    """Typed row capability policy.

    A per-row detail-open step must identify the current row somehow. A row URL/link is the most
    deterministic way to drill into a detail page, but using a stable row identity such as ID/SKU is
    still an executable strategy for runtimes that can locate the row. Treat URL omissions as
    advisory feedback; treat missing row references as structural errors.
    """

    if not row_fields:
        return
    url_fields = {field for field in row_fields if _is_url_capability(field)}
    url_keys = {_field_key(field) for field in url_fields}

    def _walk(seq: list[Stmt]) -> None:
        for item in seq:
            if isinstance(item, RunLike) and _run_looks_like_detail_open(item):
                refs = template_fields_for_var(_run_text(item), loop.var)
                ref_keys = {_field_key(field) for field in refs}
                if not refs:
                    issues.add(
                        "FOREACH_DETAIL_OPEN_NO_ROW_REFERENCE",
                        f"foreach 行「{loop.var}」提供了字段 {sorted(row_fields)}，"
                        f"但逐行详情打开步骤「{item.name}」没有引用当前行模板。"
                        f"请在该步骤中使用 {{{loop.var}[<字段>]}} 来定位本轮行；"
                        "可以用 URL/link，也可以用稳定的 ID/SKU/名称等行身份字段。",
                    )
                elif url_fields and not (ref_keys & url_keys):
                    issues.add(
                        "FOREACH_ROW_URL_NOT_USED",
                        f"foreach 行「{loop.var}」提供了 URL/HREF/link 能力 {sorted(url_fields)}，"
                        f"但详情打开步骤「{item.name}」只引用了当前行的非 URL 字段 {sorted(refs)}。"
                        "逐行打开详情时优先直接使用行 URL/link（例如 {row[url]}）会更稳定；"
                        "按 ID/SKU/名称定位行也是可执行策略，但依赖当前列表和执行器定位能力。",
                        severity="warn",
                    )
            elif isinstance(item, Call):
                fn = function_defs.get(item.func)
                if fn is None or not _function_opens_detail(fn, function_defs):
                    continue
                call_row_fields: set[str] = set()
                for value in (item.args or {}).values():
                    call_row_fields.update(template_fields_for_var(str(value), loop.var))
                call_row_keys = {_field_key(field) for field in call_row_fields}
                if not call_row_fields:
                    issues.add(
                        "FOREACH_DETAIL_OPEN_NO_ROW_REFERENCE",
                        f"foreach 行「{loop.var}」提供了字段 {sorted(row_fields)}，"
                        f"但调用会打开详情的函数「{item.func}」时没有把任何当前行字段传入。"
                        f"请把 {{{loop.var}[<字段>]}} 作为函数参数传入，用来定位本轮行；"
                        "可以用 URL/link，也可以用稳定的 ID/SKU/名称等行身份字段。",
                    )
                elif url_fields and not (call_row_keys & url_keys):
                    issues.add(
                        "FOREACH_CALL_DROPS_ROW_URL",
                        f"foreach 行「{loop.var}」提供了 URL/HREF/link 能力 {sorted(url_fields)}，"
                        f"但调用会打开详情的函数「{item.func}」时只传入了非 URL 行字段 {sorted(call_row_fields)}。"
                        "建议把 URL/link 字段作为函数参数传入，并在函数的详情打开步骤中优先使用它；"
                        "按行身份字段定位详情也是可执行 fallback。",
                        severity="warn",
                    )
            elif isinstance(item, If):
                _walk(item.then)
                _walk(item.otherwise)
            elif isinstance(item, ForEach):
                _walk(item.body)

    _walk(loop.body)


def _body_declared_fields(seq: list[Stmt], function_returns: dict[str, set[str]]) -> set[str]:
    fields: set[str] = set()
    for item in seq:
        if isinstance(item, RunLike):
            fields.update(field for field in item.returns if field)
        elif isinstance(item, Compute):
            if item.var:
                fields.add(item.var)
        elif isinstance(item, Call):
            fields.update(function_returns.get(item.func, set()))
        elif isinstance(item, If):
            fields.update(_body_declared_fields(item.then, function_returns))
            fields.update(_body_declared_fields(item.otherwise, function_returns))
        elif isinstance(item, ForEach):
            fields.update(_body_declared_fields(item.body, function_returns))
    return fields


def check_function_contract(
    fn: FunctionDef,
    function_defs: dict[str, FunctionDef],
    function_returns: dict[str, set[str]],
    issues: IssueList,
) -> None:
    produced = _body_declared_fields(fn.body, function_returns)
    missing = {field for field in fn.returns if field and field not in produced}
    if missing:
        issues.add(
            "FUNCTION_RETURN_NOT_PRODUCED",
            f"函数「{fn.name}」声明 returns={list(fn.returns)}，但 body 没有通过 Run.returns、Compute.var "
            f"或被调函数 returns 产出字段 {sorted(missing)}；运行时这些字段会变成空值。"
            "请在函数体中显式读取/计算这些字段，或从函数 returns 中删除它们。"
        )

    url_params = {param for param in fn.params if _is_url_capability(param)}
    if not url_params:
        return
    non_url_params = {param for param in fn.params if param not in url_params}

    def _walk(seq: list[Stmt]) -> None:
        for item in seq:
            if isinstance(item, RunLike):
                if (
                    item.returns
                    and _run_looks_like_detail_open(item)
                    and _run_references_any_param(item, non_url_params)
                    and not _run_uses_any_url_param(item, url_params)
                ):
                    issues.add(
                        "FUNCTION_URL_PARAM_NOT_USED",
                        f"函数「{fn.name}」有 URL/HREF/link 参数 {sorted(url_params)}，"
                        f"但详情打开/读取步骤「{item.name}」用非 URL 参数定位且没有使用 URL 参数。"
                        "建议详情入口优先打开该 URL/link；其它字段定位也是可执行 fallback，但更依赖当前页面状态。",
                        severity="warn",
                    )
            elif isinstance(item, If):
                _walk(item.then)
                _walk(item.otherwise)
            elif isinstance(item, ForEach):
                _walk(item.body)

    _walk(fn.body)
