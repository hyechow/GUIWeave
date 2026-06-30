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

from gui_agent.core.orchestrator.program import Call, Compute, Cond, Finish, ForEach, FunctionDef, If, Program, Run
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


def _codes(program: Program) -> set[str]:
    issues = validate_program(program)
    assert all(isinstance(i, ValidationIssue) for i in issues), "validate_program must return ValidationIssue"
    return {i.code for i in issues}


# One minimal program per code that makes exactly that rule fire (others may co-fire; we assert
# the target is among them — "at least one hitting sample" per the governance contract).
def _read(var="v", returns=("a",), spec="读取字段", name="读取", **kw):
    return Run(var=var, name=name, kind="read", returns=list(returns), read_spec=spec, **kw)


SAMPLES: dict[str, Program] = {
    "EMPTY_PROGRAM": Program(statements=[]),
    "NO_RESULT_SOURCE": Program(goal="有多少订单", statements=[Run(name="进入页面", kind="navigation")]),
    "TEMPLATE_VAR_NOT_IN_SCOPE": Program(statements=[Finish(message="结果是 {x[f]}")]),
    "TEMPLATE_FIELD_NOT_IN_RETURNS": Program(statements=[_read(returns=("a",)), Finish(message="{v[b]}")]),
    "TEMPLATE_BARE_VAR": Program(statements=[_read(returns=("a",)), Finish(message="值是 {v}")]),
    "TEMPLATE_UNSUPPORTED_EXPR": Program(statements=[Finish(message="结果 {x + y}")]),
    "PRECONDITION_NOT_NAVIGATION": Program(statements=[Run(name="点击保存", kind="action", precondition=True)]),
    "READ_MISSING_RETURNS": Program(statements=[Run(var="v", name="读取", kind="read")]),
    "READ_MISSING_VAR": Program(statements=[Run(name="读取", kind="read", returns=["a"], read_spec="读")]),
    "DATA_QUERY_MISSING_RETURNS": Program(statements=[Run(var="q", name="查询", kind="data_query", sql="SELECT 1")]),
    "DATA_QUERY_MISSING_VAR": Program(statements=[Run(name="查询", kind="data_query", returns=["a"], sql="SELECT 1")]),
    "DATA_QUERY_MISSING_SQL": Program(statements=[Run(var="q", name="查询", kind="data_query", returns=["a"], sql="")]),
    "DATA_QUERY_SQL_TEMPLATE_REF": Program(statements=[
        Run(var="q", name="查询", kind="data_query", returns=["a"], sql="SELECT {x[y]} FROM data"),
    ]),
    "DATA_QUERY_VAR_AS_TABLE": Program(statements=[
        _read(var="orders", returns=("a",)),
        Run(var="q", name="查询", kind="data_query", returns=["b"], sql="SELECT * FROM orders"),
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
        Run(var="v", name="把最近 3 笔订单的金额相加", kind="read", returns=["total"],
            read_spec="对最近 3 笔订单求和"),
    ]),
    "TABLE_ROW_FIELD_COLLECTION": Program(goal="统计最近订单总额", statements=[
        Run(var="v", name="读取表格可见行的字段", kind="read", returns=["grand_total", "status"],
            read_spec="读取每一行的金额和状态"),
    ]),
    "SQL_SCHEMA_MAPPING_TEXT": Program(statements=[
        Run(var="q", name="查询", kind="data_query", returns=["a"], sql="SELECT Email->customer_email FROM data"),
    ]),
    "SQL_QUOTED_DISPLAY_IDENTIFIER": Program(statements=[
        Run(var="q", name="查询", kind="data_query", returns=["a"], sql='SELECT "Customer Email" FROM data'),
    ]),
    "RANK_QUERY_DROPS_TIES": Program(goal="完成订单数第二多的客户", statements=[
        Run(var="q", name="查询第二多", kind="data_query", returns=["email"],
            sql="SELECT email, COUNT(*) FROM data GROUP BY email ORDER BY 2 DESC LIMIT 1 OFFSET 1"),
    ]),
    "AGGREGATE_LIMIT_AFTER_AGGREGATION": Program(statements=[
        Run(var="q", name="求和", kind="data_query", returns=["total"],
            sql="SELECT SUM(amount_num) AS total FROM data LIMIT 2"),
    ]),
    "TEMPORAL_LIMIT_WITHOUT_ORDER": Program(goal="最近 2 笔订单", statements=[
        Run(var="q", name="取最近2笔", kind="data_query", returns=["amount_num"],
            sql="SELECT amount_num FROM data LIMIT 2"),
    ]),
    "TEMPORAL_AGGREGATE_WITHOUT_ROW_LIMIT": Program(goal="最近 2 笔订单总额", statements=[
        Run(var="q", name="最近2笔求和", kind="data_query", returns=["total"],
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
        Run(var="rows", name="逐行采集订单", kind="read", returns=["id"], read_spec="逐行读取每条记录的 id"),
        Run(var="q", name="求和", kind="data_query", returns=["total"],
            sql="SELECT SUM(amount_num) AS total FROM data"),
    ]),
    "FOREACH_DQ_UNKNOWN_TABLE": Program(statements=[
        Run(var="rows", name="逐行采集订单", kind="read", returns=["id"], read_spec="逐行读取每条记录的 id"),
        Run(var="q", name="查询", kind="data_query", returns=["total"],
            sql="SELECT SUM(amount_num) AS total FROM mystery_table"),
    ]),
    "FOREACH_DQ_GRID_FIELD_MISSING": Program(statements=[
        ForEach(var="o", returns=["id"], into="orders", body=[]),
        Run(var="q", name="求和", kind="data_query", returns=["total"],
            sql="SELECT SUM(grand_total_num) AS total FROM orders"),
    ]),
    "FOREACH_DQ_DETAIL_FIELD_MISSING": Program(statements=[
        ForEach(var="o", returns=["id"], into="orders",
                body=[Run(var="d", name="打开详情", kind="read", returns=["x"], read_spec="读详情")]),
        Run(var="q", name="筛选", kind="data_query", returns=["total"],
            sql="SELECT SUM(amount_num) AS total FROM orders"),
    ]),
    "FOREACH_DQ_POST_FOREACH_FIELD_MISSING": Program(statements=[
        ForEach(var="o", returns=["id"], into="orders", body=[]),
        Run(var="q", name="求和", kind="data_query", returns=["total"],
            sql="SELECT SUM(amount_num) AS total FROM data"),
    ]),
}


@pytest.mark.parametrize("code", sorted(ALL_CODES))
def test_every_code_has_triggering_sample(code):
    assert code in SAMPLES, f"no triggering sample registered for {code}"
    fired = _codes(SAMPLES[code])
    assert code in fired, f"sample for {code} fired {sorted(fired)} instead"


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
