"""URL/link capability validation rules for orchestrator programs."""

from __future__ import annotations

import re

from ..program import BARE_REF_RE, TEMPLATE_RE, Call, Compute, ForEach, FunctionDef, If, Run, RunLike, Stmt
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
    """Typed row capability policy: if a row exposes URL/HREF/link, detail opening must use it."""

    url_fields = {field for field in row_fields if _is_url_capability(field)}
    if not url_fields:
        return
    url_keys = {_field_key(field) for field in url_fields}

    def _walk(seq: list[Stmt]) -> None:
        for item in seq:
            if isinstance(item, RunLike) and _run_looks_like_detail_open(item):
                refs = template_fields_for_var(_run_text(item), loop.var)
                ref_keys = {_field_key(field) for field in refs}
                if refs and not (ref_keys & url_keys):
                    issues.add(
                        "FOREACH_ROW_URL_NOT_USED",
                        f"foreach 行「{loop.var}」提供了 URL/HREF/link 能力 {sorted(url_fields)}，"
                        f"但详情打开步骤「{item.name}」只引用了当前行的非 URL 字段 {sorted(refs)}。"
                        "逐行打开详情时必须直接使用行 URL/link（例如 {row[url]}），不要依赖当前列表仍停在同一结果集后再按文本点行。"
                    )
            elif isinstance(item, Call):
                fn = function_defs.get(item.func)
                if fn is None or not _function_opens_detail(fn, function_defs):
                    continue
                call_row_fields: set[str] = set()
                for value in (item.args or {}).values():
                    call_row_fields.update(template_fields_for_var(str(value), loop.var))
                call_row_keys = {_field_key(field) for field in call_row_fields}
                if call_row_fields and not (call_row_keys & url_keys):
                    issues.add(
                        "FOREACH_CALL_DROPS_ROW_URL",
                        f"foreach 行「{loop.var}」提供了 URL/HREF/link 能力 {sorted(url_fields)}，"
                        f"但调用会打开详情的函数「{item.func}」时只传入了非 URL 行字段 {sorted(call_row_fields)}。"
                        "请把 URL/link 字段作为函数参数传入，并在函数的详情打开步骤中使用它。"
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
                        "如果调用方已经传入行 URL/link，详情入口必须打开该 URL/link；其它字段只应用于验收或 fallback 判别。"
                    )
            elif isinstance(item, If):
                _walk(item.then)
                _walk(item.otherwise)
            elif isinstance(item, ForEach):
                _walk(item.body)

    _walk(fn.body)
