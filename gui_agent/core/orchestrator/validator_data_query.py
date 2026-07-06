"""Foreach/data_query validation rules for orchestrator programs."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .program import TEMPLATE_RE, Call, Compute, ForEach, If, Run, RunLike, Stmt
from .validator_issue import IssueList
from .sql_utils import sql_identifier
from .validator_sql import (
    SQL_NON_FIELD_TOKENS,
    data_query_field_tokens,
    missing_query_fields,
    query_field_available,
    sql_cte_names,
    sql_derived_identifier_tokens,
    sql_referenced_tables,
)
from .validator_url import template_fields_for_var

_BODY_GOAL_MEMBERSHIP_RE = re.compile(
    r"判断|是否|若是|如果|属于|匹配|目标集合|目标规格|筛选成员|\bif\b|\bwhether\b|\bmember\b|\bbelongs\b|\bmatching\b",
    re.IGNORECASE,
)
_EMAIL_RESULT_RE = re.compile(r"\b(?:e-?mail|mail)\b|邮箱", re.IGNORECASE)


def _read_looks_like_row_collection(run: Run) -> bool:
    """True for read steps that describe per-row collection (old-style; should now be foreach)."""
    if run.kind != "read" or not run.returns:
        return False
    text = f"{run.name}\n{run.read_spec}".lower()
    return any(
        marker in text
        for marker in (
            "逐行", "每行", "每一行", "每条记录", "每条", "行对象", "row object", "one object per row",
        )
    )


def _asks_for_email_source(goal_text: str, run: Run) -> bool:
    text = "\n".join([
        goal_text or "",
        run.name or "",
        run.success_condition or "",
        " ".join(str(item or "") for item in (run.returns or [])),
    ])
    return bool(_EMAIL_RESULT_RE.search(text))


def _has_email_field(fields: set[str]) -> bool:
    for field in fields:
        text = str(field or "").lower()
        if "email" in text or "mail" in text or "邮箱" in text:
            return True
    return False


def check_foreach_data_query(
    stmts: list[Stmt],
    issues: IssueList,
    function_returns: dict[str, set[str]] | None = None,
    *,
    goal_text: str = "",
) -> None:
    """Guard foreach/data_query sequencing.

    1. Old-style row-collection read -> data_query without a foreach is rejected.
    2. data_query must only reference tables produced by a preceding foreach.
    3. data_query on a foreach into-table must only use fields the foreach produced.
    """

    function_returns = function_returns or {}

    @dataclass(frozen=True)
    class _ForeachTableInfo:
        label: str
        fields: set[str]
        body_empty: bool
        conditional_body_goal: bool
        row_fields: set[str]
        output_fields: set[str]

    foreach_tables: dict[str, _ForeachTableInfo] = {}

    def _fields_to_sql(fields: list[str] | set[str] | tuple[str, ...]) -> set[str]:
        return {
            ident for field in fields
            if (ident := sql_identifier(field))
        }

    def _loop_template_fields(loop: ForEach) -> set[str]:
        fields = template_fields_for_var(loop.body_goal, loop.var)
        if fields:
            return fields
        names = {v for v, _ in TEMPLATE_RE.findall(loop.body_goal or "")}
        if len(names) == 1:
            alias = next(iter(names))
            return template_fields_for_var(loop.body_goal, alias)
        return set()

    def _foreach_explicit_output_fields(loop: ForEach) -> set[str]:
        if loop.body_goal and not loop.body and loop.output_fields:
            return _fields_to_sql(loop.output_fields)
        if loop.body_goal and not loop.body:
            # Legacy body_goal plans used returns as the per-row output contract.
            return _fields_to_sql(loop.returns)
        return set()

    def _body_result_fields(seq: list[Stmt], row_fields: set[str]) -> set[str]:
        fields = set(row_fields)
        for item in seq:
            if isinstance(item, RunLike) and item.returns:
                fields.update(_fields_to_sql(item.returns))
            elif isinstance(item, Compute):
                fields.update(_fields_to_sql([item.var]))
            elif isinstance(item, Call):
                fields.update(_fields_to_sql(function_returns.get(item.func, set())))
            elif isinstance(item, If):
                fields.update(_body_result_fields(item.then, row_fields))
                fields.update(_body_result_fields(item.otherwise, row_fields))
        return fields

    def _where_field_tokens(sql: str, ignored: set[str] | None = None) -> set[str]:
        match = re.search(
            r"\bwhere\b(?P<body>.*?)(?:\bgroup\s+by\b|\border\s+by\b|\blimit\b|\boffset\b|$)",
            sql or "",
            flags=re.I | re.S,
        )
        if not match:
            return set()
        text = re.sub(r"'[^']*'", " ", match.group("body"))
        ignored = ignored or set()
        return {
            token for raw in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", text)
            if (token := raw.lower())
            and token not in ignored
            and token not in SQL_NON_FIELD_TOKENS
            and not re.fullmatch(r"table_\d+", token)
        }

    def _where_uses_output_field(sql: str, output_fields: set[str], ignored: set[str] | None = None) -> bool:
        if not output_fields:
            return False
        return any(
            query_field_available(token, output_fields)
            for token in _where_field_tokens(sql, ignored)
        )

    # Track ALL read vars (list-like or not) for foreach row_fields inference.
    # list-like reads also trigger direct-query guards; plain reads only provide row fields.
    all_read_vars: dict[str, tuple[str, set[str]]] = {}

    def _walk_seq(seq: list[Stmt], pending: dict[str, tuple[str, set[str]]]) -> None:
        local = dict(pending)
        for s in seq:
            if isinstance(s, RunLike):
                if s.kind == "read" and s.var and s.returns:
                    # Track all read vars for foreach row_fields inference.
                    all_read_vars[s.var] = (s.name, _fields_to_sql(s.returns))
                    if _read_looks_like_row_collection(s):
                        local[s.var] = (s.name, _fields_to_sql(s.returns))
                        continue
                if s.kind == "data_query":
                    needed = data_query_field_tokens(s)
                    if local and needed:
                        for var, (read_name, row_fields) in local.items():
                            missing = missing_query_fields(needed, row_fields)
                            if missing:
                                issues.add("FOREACH_DQ_ROW_FIELD_MISSING",
                                    f"data_query 步「{s.name}」紧跟逐行采集 read「{read_name}」后，"
                                    f"使用/返回了该行未读出的字段 {sorted(missing)}；"
                                    "若这些字段只在每条记录详情里，必须先插入 foreach（name 描述集合，returns 列出每行字段）"
                                    "，body 里写打开详情的 run 并通过 returns/read_spec 产出详情字段，"
                                    "foreach 用 into 产出汇总表；然后 data_query 只能查询该 into 表。"
                                    "不要跳过 foreach 凭空查询详情字段。"
                                )
                                break
                    refs = sql_referenced_tables(s.sql) - sql_cte_names(s.sql)
                    unknown_refs = {
                        ref for ref in refs
                        if ref not in foreach_tables and ref != "data" and not re.fullmatch(r"table_\d+", ref)
                    }
                    if local and unknown_refs:
                        issues.add("FOREACH_DQ_UNKNOWN_TABLE",
                            f"data_query 步「{s.name}」查询了未由前序 foreach 产出的表 {sorted(unknown_refs)}；"
                            "data_query 只能查询 foreach 的 into 表。"
                            "若需要补详情字段，请先写 foreach（name/returns），body 用打开详情的 run + returns/read_spec "
                            "产出 into 表，再查询该表。"
                        )
                    for table in refs & set(foreach_tables):
                        table_info = foreach_tables[table]
                        table_label = table_info.label
                        fields = table_info.fields
                        body_empty = table_info.body_empty
                        if _asks_for_email_source(goal_text, s) and not _has_email_field(fields):
                            issues.add(
                                "EMAIL_RESULT_WITHOUT_EMAIL_SOURCE",
                                f"data_query 步「{s.name}」要返回 email/mail/邮箱 类结果，"
                                f"但它查询的 foreach 表「{table_label}」没有任何 email/mail/邮箱 语义字段；"
                                "不能把 customer/name/billing 等非邮箱列用 `AS customer_email` 冒充邮箱。"
                                "请让 foreach row_fields/returns 包含真实邮箱字段，"
                                "或在 foreach body 中打开详情读取真实邮箱后再查询。"
                            )
                            break
                        # Exclude data_query returns from the check: they are output aliases
                        # (e.g. "SUM(...) AS total"), not fields that must come from the foreach table.
                        returns_aliases = {str(r).strip().lower() for r in (s.returns or [])}
                        missing = missing_query_fields(needed, fields, refs | returns_aliases)
                        if missing:
                            if body_empty:
                                issues.add("FOREACH_DQ_GRID_FIELD_MISSING",
                                    f"data_query 步「{s.name}」查询 foreach body=[] 产出的网格表「{table_label}」，"
                                    f"但 SQL 需要的字段 {sorted(missing)} 没有被该 foreach row_fields/returns 采集；"
                                    "请把这些字段的基础网格列加入 foreach row_fields（旧计划可加入 foreach returns；例如需要 *_ts 就采集对应日期/时间列，"
                                    "需要 *_num 就采集对应金额/数字列），不要因此改成逐条钻取。"
                                )
                            else:
                                issues.add("FOREACH_DQ_DETAIL_FIELD_MISSING",
                                    f"data_query 步「{s.name}」查询 foreach 产出的表「{table_label}」，"
                                    f"但使用/返回了 foreach body 没有通过 returns 产出的字段 {sorted(missing)}；"
                                    "请在该 foreach body 里逐条打开详情，并让打开详情的 run 带 returns/read_spec 产出这些字段；"
                                    "若该 foreach 使用 body_goal，请把每行子目标会返回/计算出的字段列入 output_fields，"
                                    "再对 into 表 data_query。"
                                )
                            break
                        if table_info.conditional_body_goal and not _where_uses_output_field(
                            s.sql,
                            table_info.output_fields - table_info.row_fields,
                            refs | returns_aliases | sql_derived_identifier_tokens(s.sql),
                        ):
                            issues.add(
                                "FOREACH_BODY_GOAL_QUERY_ROW_PREDICATE",
                                f"data_query 步「{s.name}」查询 body_goal 产出的表「{table_label}」，"
                                "但 WHERE 没有使用 body_goal 产出的结果字段，而是在 foreach 行字段上继续做成员筛选。"
                                "body_goal 已负责运行时判断成员（如是否 size 28/是否匹配目标集合）；"
                                "后续 SQL 应筛 body_goal 明确产出的字段，例如 `status = 'updated'`、"
                                "`is_member = 'yes'`、`size = '28'`，或 `old_price != '' AND new_price != ''`。"
                                "不要用 `sku LIKE '%28%'`、`name LIKE ...` 这类分解时猜出来的字面谓词二次筛选，"
                                "否则会漏掉真实编码不含这些字面的成员。"
                            )
                            break
                    if foreach_tables and not (refs & set(foreach_tables)):
                        produced: set[str] = set()
                        labels: list[str] = []
                        for info in foreach_tables.values():
                            labels.append(info.label)
                            produced.update(info.fields)
                        missing = missing_query_fields(needed, produced, refs)
                        if missing:
                            issues.add("FOREACH_DQ_POST_FOREACH_FIELD_MISSING",
                                f"data_query 步「{s.name}」位于 foreach 之后，但使用/返回了此前 foreach "
                                f"没有通过 row_fields/returns/output_fields 产出的字段 {sorted(missing)}；已存在的 foreach 表为 {labels}。"
                                "若要按每条记录详情字段筛选，请在 foreach body 中用打开详情的 run 返回这些字段，"
                                "或在 body_goal 的 output_fields 中声明这些字段，"
                                "并让 SQL 查询对应的 into 表。"
                            )
            elif isinstance(s, ForEach):
                row_fields = set()
                # Use list-like source fields first; fall back to any read var with matching name.
                if s.over in local:
                    row_fields = set(local[s.over][1])
                elif s.over in all_read_vars:
                    row_fields = set(all_read_vars[s.over][1])
                if s.row_fields:
                    row_fields.update(_fields_to_sql(s.row_fields))
                elif s.body_goal and not s.body:
                    row_fields.update(_fields_to_sql(_loop_template_fields(s)))
                elif s.returns:
                    # new-style foreach: collect_fn provides rows with these fields.
                    # Normalize with _sql_identifier (same as runtime) so "Grand Total (Purchased)"
                    # maps to "grand_total_purchased" and matches what data_query SQL writes.
                    row_fields.update(_fields_to_sql(s.returns))
                output_fields = _foreach_explicit_output_fields(s)
                fields = _body_result_fields(s.body, row_fields)
                fields.update(output_fields)
                table_name = (s.into or f"{s.var}s").lower()
                foreach_tables[table_name] = _ForeachTableInfo(
                    label=s.into or f"{s.var}s",
                    fields=fields,
                    body_empty=not bool(s.body) and not bool(s.body_goal),
                    conditional_body_goal=bool(
                        s.body_goal
                        and not s.body
                        and _BODY_GOAL_MEMBERSHIP_RE.search(s.body_goal or "")
                    ),
                    row_fields=set(row_fields),
                    output_fields=set(output_fields),
                )
                if s.over in local:
                    local.pop(s.over, None)
                _walk_seq(s.body, {})
            elif isinstance(s, If):
                _walk_seq(s.then, dict(local))
                _walk_seq(s.otherwise, dict(local))

    _walk_seq(stmts, {})
