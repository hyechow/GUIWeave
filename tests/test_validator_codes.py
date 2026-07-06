"""Governance tests for the validator's coded issues.

The point of ValidationIssue.code is measurement: every rule must be (a) registered in
ALL_CODES with no drift, and (b) reachable by at least one program (no dead rule). These
tests are the regression gate for that — they let us tell "this rule fired" apart from
"the LLM was flaky" when reading run logs, which the old free-text issues couldn't.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from gui_agent.core.orchestrator.program import Call, Compute, Cond, Finish, ForEach, FunctionDef, If, Program, Read, Query, Run
from gui_agent.core.orchestrator.validator import ALL_CODES, IssueList, ValidationIssue, validate_program

_VALIDATOR_SRC = Path("gui_agent/core/orchestrator/validator.py")


def _emitted_codes() -> set[str]:
    """Statically harvest every code literal passed to issues.add(...) / IssueList.one(...)."""
    tree = ast.parse(_VALIDATOR_SRC.read_text())
    codes: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in {"add", "one"}:
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        if isinstance(node.args[0].value, str):
            codes.add(node.args[0].value)
    return codes


def test_emitted_codes_match_registry():
    """No drift: the codes actually emitted in source == the ALL_CODES registry, both ways.

    A code emitted but unregistered (or registered but never emitted) fails here, forcing the
    registry to stay the single source of truth as rules are added/removed."""
    emitted = _emitted_codes()
    assert emitted == set(ALL_CODES), {
        "emitted_but_unregistered": sorted(emitted - set(ALL_CODES)),
        "registered_but_unemitted": sorted(set(ALL_CODES) - emitted),
    }


def _codes(sample) -> set[str]:
    program, resolution = sample if isinstance(sample, tuple) else (sample, None)
    issues = validate_program(program, resolution=resolution)
    assert all(isinstance(i, ValidationIssue) for i in issues), "validate_program must return ValidationIssue"
    return {i.code for i in issues}


# One minimal program per code that makes exactly that rule fire (others may co-fire; we assert
# the target is among them — "at least one hitting sample" per the governance contract).
def _read(var="v", returns=("a",), spec="读取字段", name="读取", **kw):
    return Read(var=var, name=name,  returns=list(returns), read_spec=spec, **kw)


SAMPLES: dict[str, Program] = {
    "EMPTY_PROGRAM": Program(statements=[]),
    # S10a: a step whose own acceptance says nothing changes — invented flow control (185 sample)
    "NOOP_FLOW_CONTROL_STEP": Program(statements=[
        Run(name="保存当前行结果（逻辑上）", kind="action",
            success_condition="无UI变化，仅用于流程控制"),
    ]),
    # S10b: bare attribute access is runtime-fatal in the compute dialect (185 sample: row.sku)
    "COMPUTE_UNSUPPORTED_EXPR": Program(statements=[
        _read(var="row", returns=("sku",)),
        Compute(var="base", expr="row.sku.rsplit('-', 2)[0]"),
        Run(name="搜索 {base}", kind="filter"),
    ]),
    # S10b: name not visible at this point on this path
    "COMPUTE_UNKNOWN_NAME": Program(statements=[
        Compute(var="x", expr="ghost_field.strip()"),
        Run(name="使用 {x}", kind="action"),
    ]),
    "NO_RESULT_SOURCE": Program(goal="有多少订单", statements=[Run(name="进入页面", kind="navigation")]),
    "TEMPLATE_VAR_NOT_IN_SCOPE": Program(statements=[Finish(message="结果是 {x[f]}")]),
    "TEMPLATE_FIELD_NOT_IN_RETURNS": Program(statements=[_read(returns=("a",)), Finish(message="{v[b]}")]),
    "TEMPLATE_BARE_VAR": Program(statements=[_read(returns=("a",)), Finish(message="值是 {v}")]),
    "TEMPLATE_UNSUPPORTED_EXPR": Program(statements=[Finish(message="结果 {x + y}")]),
    # 778 regression: computed value never referenced downstream → the fill action has no concrete
    # target and the planner hallucinates one (filled 150.00 instead of the computed 86.50).
    "COMPUTE_VAR_UNUSED": Program(statements=[
        Compute(var="new_price", expr="round(75 * 0.865, 2)"),
        Run(name="将价格更新为新值并保存", kind="action"),
    ]),
    # offline 778 v4: a "Reduce the price" (mutate) goal decomposed into collect+classify only — a
    # foreach body_goal judging size-28 membership, no action step anywhere → can never mutate.
    "MUTATE_GOAL_WITHOUT_ACTION": Program(goal="Reduce the price of size 28 Sahara leggings by 13.5%", statements=[
        Run(name="进入产品列表", kind="navigation"),
        ForEach(var="row", target="Sahara 行", returns=["sku", "action_url"],
                body_goal="从 {row[sku]} 判断是否为 size 28 的变体；若是返回 action_url，否则标记为空"),
    ]),
    "PRECONDITION_NOT_NAVIGATION": Program(statements=[Run(name="点击保存", kind="action", precondition=True)]),
    "READ_MISSING_RETURNS": Program(statements=[Read(var="v", name="读取")]),
    "READ_MISSING_VAR": Program(statements=[Read(name="读取",  returns=["a"], read_spec="读")]),
    "DATA_QUERY_MISSING_RETURNS": Program(statements=[Query(var="q", name="查询",  sql="SELECT 1")]),
    "DATA_QUERY_MISSING_VAR": Program(statements=[Query(name="查询",  returns=["a"], sql="SELECT 1")]),
    "DATA_QUERY_MISSING_SQL": Program(statements=[Query(var="q", name="查询",  returns=["a"], sql="")]),
    "DATA_QUERY_SQL_TEMPLATE_REF": Program(statements=[
        Query(var="q", name="查询",  returns=["a"], sql="SELECT {x[y]} FROM data"),
    ]),
    "DATA_QUERY_VAR_AS_TABLE": Program(statements=[
        _read(var="orders", returns=("a",)),
        Query(var="q", name="查询",  returns=["b"], sql="SELECT * FROM orders"),
    ]),
    "RETURNS_WITHOUT_VAR": Program(statements=[Run(name="点击", kind="action", returns=["a"], read_spec="读")]),
    "RETURNS_WITHOUT_READ_SPEC": Program(statements=[Run(var="v", name="点击", kind="action", returns=["a"])]),
    "CALL_FUNC_NOT_DEFINED": Program(statements=[Call(func="missing", args={}, var="m")]),
    "CALL_RETURNS_WITHOUT_VAR": Program(
        functions=[FunctionDef(name="f", returns=["x"], body=[Compute(var="x", expr="'1'")])],
        statements=[Call(func="f", args={})],
    ),
    "FUNCTION_RETURN_NOT_PRODUCED": Program(
        functions=[FunctionDef(name="f", returns=["x"], body=[Compute(var="y", expr="'1'")])],
        statements=[Call(func="f", args={}, var="m")],
    ),
    "FUNCTION_URL_PARAM_NOT_USED": Program(
        functions=[FunctionDef(name="f", params=["sku", "detail_url"], returns=["material"], body=[
            Run(var="d", name="打开 SKU={sku} 详情页", kind="navigation", returns=["material"], read_spec="读 material"),
        ])],
        statements=[Call(func="f", args={"sku": "ABC", "detail_url": "https://example.test/detail"}, var="m")],
    ),
    "VISUAL_ROW_AGGREGATION": Program(statements=[
        Read(var="v", name="把最近 3 笔订单的金额相加",  returns=["total"],
            read_spec="对最近 3 笔订单求和"),
    ]),
    "TABLE_ROW_FIELD_COLLECTION": Program(goal="统计最近订单总额", statements=[
        Read(var="v", name="读取表格可见行的字段",  returns=["grand_total", "status"],
            read_spec="读取每一行的金额和状态"),
    ]),
    "SQL_SCHEMA_MAPPING_TEXT": Program(statements=[
        Query(var="q", name="查询",  returns=["a"], sql="SELECT Email->customer_email FROM data"),
    ]),
    "SQL_QUOTED_DISPLAY_IDENTIFIER": Program(statements=[
        Query(var="q", name="查询",  returns=["a"], sql='SELECT "Customer Email" FROM data'),
    ]),
    "RANK_QUERY_DROPS_TIES": Program(goal="完成订单数第二多的客户", statements=[
        Query(var="q", name="查询第二多",  returns=["email"],
            sql="SELECT email, COUNT(*) FROM data GROUP BY email ORDER BY 2 DESC LIMIT 1 OFFSET 1"),
    ]),
    "AGGREGATE_LIMIT_AFTER_AGGREGATION": Program(statements=[
        Query(var="q", name="求和",  returns=["total"],
            sql="SELECT SUM(amount_num) AS total FROM data LIMIT 2"),
    ]),
    "TEMPORAL_LIMIT_WITHOUT_ORDER": Program(goal="最近 2 笔订单", statements=[
        Query(var="q", name="取最近2笔",  returns=["amount_num"],
            sql="SELECT amount_num FROM data LIMIT 2"),
    ]),
    "TEMPORAL_AGGREGATE_WITHOUT_ROW_LIMIT": Program(goal="最近 2 笔订单总额", statements=[
        Query(var="q", name="最近2笔求和",  returns=["total"],
            sql="SELECT SUM(amount_num) AS total FROM data"),
    ]),
    "IF_COND_VAR_NOT_IN_SCOPE": Program(statements=[
        If(cond=Cond(var="x", field="f", cmp="==", value="1"), then=[Finish(message="ok")]),
    ]),
    "IF_COND_FIELD_NOT_IN_RETURNS": Program(statements=[
        _read(returns=("a",)),
        If(cond=Cond(var="v", field="b", cmp="==", value="1"), then=[Finish(message="ok")]),
    ]),
    "IF_COND_MISSING_VALUE": Program(statements=[
        _read(returns=("a",)),
        If(cond=Cond(var="v", field="a", cmp="contains", value=""), then=[Finish(message="ok")]),
    ]),
    "IF_COND_MISSING_VALUES": Program(statements=[
        _read(returns=("a",)),
        If(cond=Cond(var="v", field="a", cmp="in", values=[]), then=[Finish(message="ok")]),
    ]),
    # live 778 shape: `count == '0'` (empty guard) with the WORK under then and the not-found finish
    # under else — at runtime count='3' → else → finished "未找到" with zero saves.
    "IF_EMPTY_GUARD_INVERTED": Program(statements=[
        _read(returns=("count",)),
        If(
            cond=Cond(var="v", field="count", cmp="==", value="0"),
            then=[Run(name="打开变体并更新价格", kind="action")],
            otherwise=[Finish(message="未找到任何匹配变体")],
        ),
    ]),
    "FOREACH_OVER_NOT_IN_SCOPE": Program(statements=[
        ForEach(var="row", over="missing", body=[Finish(message="ok")]),
    ]),
    "FOREACH_MISSING_LOOP_VAR": Program(statements=[
        ForEach(var="", returns=["id"], body=[]),
    ]),
    "FOREACH_EMPTY_BODY_NO_RETURNS": Program(statements=[
        ForEach(var="row", body=[]),
    ]),
    "FOREACH_BODY_GOAL_MISSING_RETURNS": Program(statements=[
        ForEach(var="row", body_goal="读 {row[Name]} 的材质"),
    ]),
    "FOREACH_BODY_GOAL_NO_ROW_TEMPLATE": Program(statements=[
        ForEach(var="row", returns=["material"], body_goal="读这一行的材质"),
    ]),
    "FOREACH_CALL_DROPS_ROW_URL": Program(
        functions=[FunctionDef(name="f", params=["sku"], returns=["material"], body=[
            Run(var="d", name="打开 SKU={sku} 详情页", kind="navigation", returns=["material"], read_spec="读 material"),
        ])],
        statements=[ForEach(var="row", returns=["SKU", "Action_url"], body=[
            Call(func="f", args={"sku": "{row[SKU]}"}, var="m"),
        ])],
    ),
    "FOREACH_ROW_URL_NOT_USED": Program(statements=[
        ForEach(var="row", returns=["SKU", "Action_url"], body=[
            Run(name="打开 {row[SKU]} 详情页", kind="navigation"),
        ]),
    ]),
    "RETRIEVAL_RETRY_DROPS_FIELD": Program(statements=[
        _read(var="r", returns=("a",)),
        Run(name="在 Status 列筛选 Complete", kind="filter", success_condition="筛选完成"),
        If(cond=Cond(var="r", field="a", cmp="exists"),
           then=[Run(name="用关键词重新搜索", kind="filter", success_condition="0 条结果就放宽关键词")]),
    ]),
    "FOREACH_DQ_ROW_FIELD_MISSING": Program(statements=[
        Read(var="rows", name="逐行采集订单",  returns=["id"], read_spec="逐行读取每条记录的 id"),
        Query(var="q", name="求和",  returns=["total"],
            sql="SELECT SUM(amount_num) AS total FROM data"),
    ]),
    "FOREACH_DQ_UNKNOWN_TABLE": Program(statements=[
        Read(var="rows", name="逐行采集订单",  returns=["id"], read_spec="逐行读取每条记录的 id"),
        Query(var="q", name="查询",  returns=["total"],
            sql="SELECT SUM(amount_num) AS total FROM mystery_table"),
    ]),
    "FOREACH_DQ_GRID_FIELD_MISSING": Program(statements=[
        ForEach(var="o", returns=["id"], into="orders", body=[]),
        Query(var="q", name="求和",  returns=["total"],
            sql="SELECT SUM(grand_total_num) AS total FROM orders"),
    ]),
    "EMAIL_RESULT_WITHOUT_EMAIL_SOURCE": Program(goal="Get customer email(s) by order count", statements=[
        ForEach(var="o", returns=["ID", "Billing Customer", "Status"], into="orders", body=[]),
        Query(var="q", name="查询 customer email",  returns=["customer_email"],
            sql="SELECT billing_customer AS customer_email FROM orders"),
    ]),
    "FOREACH_DQ_DETAIL_FIELD_MISSING": Program(statements=[
        ForEach(var="o", returns=["id"], into="orders",
                body=[Read(var="d", name="打开详情",  returns=["x"], read_spec="读详情")]),
        Query(var="q", name="筛选",  returns=["total"],
            sql="SELECT SUM(amount_num) AS total FROM orders"),
    ]),
    "FOREACH_DQ_POST_FOREACH_FIELD_MISSING": Program(statements=[
        ForEach(var="o", returns=["id"], into="orders", body=[]),
        Query(var="q", name="求和",  returns=["total"],
            sql="SELECT SUM(amount_num) AS total FROM data"),
    ]),
    "FOREACH_BODY_GOAL_QUERY_ROW_PREDICATE": Program(statements=[
        ForEach(
            var="row",
            into="sahara_leggings_28_rows",
            row_fields=["sku", "name", "action_url"],
            output_fields=["sku", "old_price", "new_price"],
            body_goal=(
                "判断 {row[sku]} 是否为 size 28 的 Sahara leggings 变体；若是，"
                "打开 {row[action_url]} 读当前价、更新并返回 old_price/new_price"
            ),
        ),
        Query(
            var="q",
            name="确认结果",
            returns=["result"],
            sql=(
                "SELECT sku, old_price, new_price FROM sahara_leggings_28_rows "
                "WHERE sku LIKE '%Sahara%' AND sku LIKE '%28%'"
            ),
        ),
    ]),
}


@pytest.mark.parametrize("code", sorted(ALL_CODES))
def test_every_code_has_triggering_sample(code):
    assert code in SAMPLES, f"no triggering sample registered for {code}"
    fired = _codes(SAMPLES[code])
    assert code in fired, f"sample for {code} fired {sorted(fired)} instead"


def test_foreach_row_url_policy_checks_runs_inside_if():
    program = Program(statements=[
        ForEach(
            var="row",
            into="target_products",
            row_fields=["sku", "action_url"],
            body=[
                If(
                    cond=Cond(var="row", field="sku", cmp="contains", value="size 28"),
                    then=[
                        Run(
                            kind="navigation",
                            var="d",
                            name="打开变体 {row[sku]} 的编辑页",
                            returns=["current_price"],
                        )
                    ],
                )
            ],
        )
    ])

    assert "FOREACH_ROW_URL_NOT_USED" in _codes(program)


def test_validation_issue_is_str_with_metadata():
    issue = ValidationIssue("SOME_CODE", "人类可读消息", evidence=("step",))
    assert isinstance(issue, str) and issue == "人类可读消息"
    assert issue.code == "SOME_CODE" and issue.severity == "error" and issue.evidence == ("step",)
    assert issue.message_for_llm == "人类可读消息"


def test_issuelist_is_list_compatible():
    lst = IssueList()
    lst.add("A", "msg-a")
    assert lst == ["msg-a"] and lst[0].code == "A"
    assert IssueList() == []  # empty compares equal to plain list — keeps `validate_program(...) == []`


def test_if_empty_guard_inverted_shapes():
    from gui_agent.core.orchestrator import validate_program

    def codes(prog):
        return {i.code for i in validate_program(prog)}

    # inverted (the live 778 shape) → flagged
    bad = SAMPLES["IF_EMPTY_GUARD_INVERTED"]
    assert "IF_EMPTY_GUARD_INVERTED" in codes(bad)

    # correct shape: ==0 → not-found finish; work in else → NOT flagged
    good = Program(statements=[
        _read(returns=("count",)),
        If(
            cond=Cond(var="v", field="count", cmp="==", value="0"),
            then=[Finish(message="未找到任何匹配变体")],
            otherwise=[Run(name="打开变体并更新价格", kind="action")],
        ),
    ])
    assert "IF_EMPTY_GUARD_INVERTED" not in codes(good)

    # symmetric inversion: `!= '0'` with work under else / finish under then → flagged
    bad2 = Program(statements=[
        _read(returns=("count",)),
        If(
            cond=Cond(var="v", field="count", cmp="!=", value="0"),
            then=[Finish(message="未找到任何匹配变体")],
            otherwise=[Run(name="打开变体并更新价格", kind="action")],
        ),
    ])
    assert "IF_EMPTY_GUARD_INVERTED" in codes(bad2)

    # both branches have work (ambiguous) → not flagged
    ambiguous = Program(statements=[
        _read(returns=("count",)),
        If(
            cond=Cond(var="v", field="count", cmp="==", value="0"),
            then=[Run(name="走空态处理流程", kind="action")],
            otherwise=[Run(name="打开变体并更新价格", kind="action")],
        ),
    ])
    assert "IF_EMPTY_GUARD_INVERTED" not in codes(ambiguous)


def test_mutate_goal_without_action_shapes():
    def codes(prog):
        return {i.code for i in validate_program(prog)}

    # collect+classify only (offline 778 v4) → flagged
    assert "MUTATE_GOAL_WITHOUT_ACTION" in codes(SAMPLES["MUTATE_GOAL_WITHOUT_ACTION"])

    # mutate goal WITH an action inside the foreach body → not flagged
    good = Program(goal="Reduce the price of size 28 Sahara leggings by 13.5%", statements=[
        ForEach(var="p", target="size28 变体行", returns=["sku", "price"], body=[
            Compute(var="new_price", expr="round({p[price]} * 0.865, 2)"),
            Run(name="将价格更新为 {new_price} 并保存", kind="action"),
        ]),
    ])
    assert "MUTATE_GOAL_WITHOUT_ACTION" not in codes(good)

    # mutate goal whose body_goal TEXT carries the mutation verb → not flagged (re-decomposed at runtime)
    good_bg = Program(goal="Reduce the price of size 28 Sahara leggings by 13.5%", statements=[
        ForEach(var="p", target="size28 变体行", returns=["sku", "price"],
                body_goal="打开 {p[sku]} 详情页，读当前价格、按 0.865 计算后更新价格并保存"),
    ])
    assert "MUTATE_GOAL_WITHOUT_ACTION" not in codes(good_bg)

    # retrieve goal with no action → not flagged (not a mutation task)
    retrieve = Program(goal="Get the total number of reviews", statements=[
        Read(var="v", name="读取评论总数",  returns=["count"], read_spec="读 count"),
        Finish(message="{v[count]}"),
    ])
    assert "MUTATE_GOAL_WITHOUT_ACTION" not in codes(retrieve)
