"""Governance tests for the validator's coded issues.

The point of ValidationIssue.code is measurement: every rule must be (a) registered in
ALL_CODES with no drift, and (b) reachable by at least one program (no dead rule). These
tests are the regression gate for that — they let us tell "this rule fired" apart from
"the LLM was flaky" when reading run logs, which the old free-text issues couldn't.
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from gui_agent.core.orchestrator.program import Call, Compute, Cond, Finish, ForEach, FunctionDef, If, Program, Read, Query, Run
from gui_agent.core.orchestrator._decomposer.draft import _StepDraft
from gui_agent.core.orchestrator._validator.governance import TEXTUAL_FALLBACK_VALIDATOR_CODES
from gui_agent.core.orchestrator.validator import ALL_CODES, IssueList, ValidationIssue, validate_program

_VALIDATOR_SRCS = tuple(sorted(Path("gui_agent/core/orchestrator").glob("validator*.py"))) + tuple(
    sorted(Path("gui_agent/core/orchestrator/_validator").glob("*.py"))
)


def _emitted_codes() -> set[str]:
    """Statically harvest every code literal passed to issues.add(...) / IssueList.one(...)."""
    codes: set[str] = set()
    for src in _VALIDATOR_SRCS:
        tree = ast.parse(src.read_text())
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
    "RETURNS_WITHOUT_READ_SPEC": Program(statements=[Run(var="v", name="点击", kind="action", returns=["a"])]),
    "MUTATION_RESULT_UNUSED": Program(goal="更新现有记录", statements=[
        Run(
            var="save_result",
            name="更新字段并保存",
            kind="action",
            returns=["status"],
            read_spec="status：读取保存状态",
        ),
        Finish(message="操作结束"),
    ]),
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
    "PRESERVED_SCOPE_FILTER_MISSING_VALUE": (
        Program(statements=[
            Run(
                name="保留客户筛选结果范围，追加 Status=Pending",
                kind="filter",
                success_condition="Active filters 同时包含客户筛选和 Status=Pending",
            ),
        ]),
        SimpleNamespace(entities=[
            SimpleNamespace(mention="Grace Nguyen", search_key="Grace", type="customer"),
        ]),
    ),
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
    "SINGLE_TARGET_LIMIT_HIDES_AMBIGUITY": Program(statements=[
        Query(
            var="q",
            name="选出目标入口",
            returns=["detail_url"],
            sql="SELECT detail_url FROM candidates WHERE kind = 'owner' LIMIT 1",
        ),
        Run(name="打开 {q[detail_url]} 进入编辑页", kind="navigation"),
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
    "FOREACH_COLLECTION_UNUSED": Program(statements=[
        ForEach(var="row", row_fields=["order_id", "detail_url"], into="pending_orders", body=[]),
        Run(name="打开最近一笔 Pending 订单详情页", kind="navigation"),
    ]),
    "FOREACH_DETAIL_OPEN_NO_ROW_REFERENCE": Program(statements=[
        ForEach(var="row", returns=["SKU", "Action_url"], body=[
            Run(name="打开评论详情页", kind="navigation"),
        ]),
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


def test_textual_fallback_validator_codes_are_registered_and_sampled():
    assert TEXTUAL_FALLBACK_VALIDATOR_CODES <= set(ALL_CODES)
    assert TEXTUAL_FALLBACK_VALIDATOR_CODES <= set(SAMPLES)


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

    issues = validate_program(program)
    assert "FOREACH_ROW_URL_NOT_USED" in {issue.code for issue in issues}
    assert [issue.severity for issue in issues if issue.code == "FOREACH_ROW_URL_NOT_USED"] == ["warn"]


def test_foreach_detail_open_must_reference_current_row():
    issues = validate_program(SAMPLES["FOREACH_DETAIL_OPEN_NO_ROW_REFERENCE"])
    assert [issue.severity for issue in issues if issue.code == "FOREACH_DETAIL_OPEN_NO_ROW_REFERENCE"] == ["error"]


def test_url_capability_advisories_are_warnings():
    for code in ("FUNCTION_URL_PARAM_NOT_USED", "FOREACH_CALL_DROPS_ROW_URL", "FOREACH_ROW_URL_NOT_USED"):
        issues = validate_program(SAMPLES[code])
        severities = [issue.severity for issue in issues if issue.code == code]
        assert severities == ["warn"]


def test_preserved_scope_filter_accepts_concrete_entity_value():
    program = Program(statements=[
        Run(
            name="保留 Grace 客户结果范围，追加 Status=Pending",
            kind="filter",
            success_condition="Active filters 同时包含 Grace 和 Status=Pending",
        ),
    ])
    resolution = SimpleNamespace(entities=[
        SimpleNamespace(mention="Grace Nguyen", search_key="Grace", type="customer"),
    ])

    assert "PRESERVED_SCOPE_FILTER_MISSING_VALUE" not in {
        issue.code for issue in validate_program(program, resolution=resolution)
    }


def test_preserved_scope_filter_requires_lookup_value_with_multiple_entities():
    program = Program(statements=[
        Run(
            name="保留客户筛选结果范围，追加 Status=Pending",
            kind="filter",
            success_condition="Active filters 同时包含客户筛选和 Status=Pending",
        ),
    ])
    resolution = SimpleNamespace(entities=[
        SimpleNamespace(mention="Grace Nguyen", search_key="Grace", type="customer"),
        SimpleNamespace(mention="most recent pending order", search_key="pending", type="order"),
    ])

    assert "PRESERVED_SCOPE_FILTER_MISSING_VALUE" in {
        issue.code for issue in validate_program(program, resolution=resolution)
    }


def test_unused_compute_blocks_mutations_but_only_warns_for_read_only_tasks():
    read_only = Program(
        goal="Return matching records",
        statements=[
            Compute(var="filtered_rows", expr="'unused'"),
            Query(var="q", name="查询", returns=["result"], sql="SELECT result FROM data"),
            Finish(message="{q[result]}"),
        ],
    )
    mutation = Program(
        goal="Reduce the price",
        statements=[
            Compute(var="new_price", expr="round(100 * 0.865, 2)"),
            Run(name="将价格更新为新值并保存", kind="action"),
        ],
    )

    assert [i.severity for i in validate_program(read_only) if i.code == "COMPUTE_VAR_UNUSED"] == ["warn"]
    assert [i.severity for i in validate_program(mutation) if i.code == "COMPUTE_VAR_UNUSED"] == ["error"]


def test_retrieval_retry_overlap_handles_chinese_prefix():
    # Regression (live 113 run): the fallback step「清除精确值后**在产品字段**…」DOES name the same
    # field as the exact step「在产品字段…」, but the greedy Chinese capture swallowed the preceding
    # prose ("清除精确值后在产品") and the overlap check false-positived RETRIEVAL_RETRY_DROPS_FIELD.
    from gui_agent.core.orchestrator._validator.retrieval import (
        _extract_retrieval_fields, _retrieval_fields_overlap,
    )
    exact = _extract_retrieval_fields("在产品字段用精确值『Olivia zip jacket』筛选")
    fallback = _extract_retrieval_fields("清除精确值后在产品字段用关键词『Olivia』重筛并提交")
    assert exact == ["产品"]
    assert fallback == ["产品"]                              # was ["清除精确值后在产品"]
    assert _retrieval_fields_overlap(exact, fallback) is True   # same field → no false drop

    # genuine field-drop is still caught
    other = _extract_retrieval_fields("在客户字段用关键词『Olivia』重筛")
    assert _retrieval_fields_overlap(exact, other) is False

    # a field outside the bilingual dict is covered by the suffix-tolerance backstop
    eo = _extract_retrieval_fields("在订单字段用精确值筛选")
    fo = _extract_retrieval_fields("清除后订单字段用关键词重筛")
    assert _retrieval_fields_overlap(eo, fo) is True


def test_retrieval_field_extraction_ignores_search_box_location_words():
    from gui_agent.core.orchestrator._validator.retrieval import _extract_retrieval_fields

    assert _extract_retrieval_fields("在顶部搜索框输入精确值『Grace Nguyen』进行筛选") == []
    assert _extract_retrieval_fields("在顶部搜索框输入精确客户名『Grace Nguyen』并提交搜索") == []
    assert _extract_retrieval_fields("清除精确值后在同一搜索框用关键词『Grace』重筛") == []


def test_retrieval_retry_accepts_same_field_anaphora():
    program = Program(statements=[
        Run(
            var="f1",
            kind="filter",
            name="在 Bill-to Name 字段输入精确值『Grace Nguyen』并提交筛选",
            returns=["match_count"],
            read_spec="match_count：读取记录数",
        ),
        If(
            cond=Cond(var="f1", field="match_count", cmp="==", value="0"),
            then=[
                Run(
                    kind="filter",
                    name="清除精确值后在同一字段输入关键词『Grace』并提交筛选",
                ),
            ],
        ),
    ])

    assert "RETRIEVAL_RETRY_DROPS_FIELD" not in {
        issue.code for issue in validate_program(program)
    }


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


def test_compute_scalar_condition_may_use_empty_field():
    program = Program(statements=[
        Compute(var="is_target", expr="1 == 1"),
        If(
            cond=Cond(var="is_target", field="", cmp="==", value="true"),
            then=[Finish(message="ok")],
        ),
    ])

    codes = _codes(program)
    assert "IF_COND_VAR_NOT_IN_SCOPE" not in codes
    assert "IF_COND_FIELD_NOT_IN_RETURNS" not in codes
    assert "COMPUTE_VAR_UNUSED" not in codes


def test_unbound_returns_do_not_count_as_result_source():
    program = Program(
        goal="Return customer email",
        statements=[
            Run(name="点击并读一个值但不绑定", kind="action", returns=["answer"], read_spec="读 answer"),
        ],
    )

    assert "NO_RESULT_SOURCE" in _codes(program)


def test_compute_consumed_by_foreach_output_and_sql_is_not_dead():
    program = Program(statements=[
        ForEach(
            var="row",
            into="updates",
            row_fields=["price"],
            output_fields=["new_price"],
            body=[
                Compute(var="new_price", expr="round(float(row['price']) * 0.865, 2)"),
            ],
        ),
        Query(
            var="q",
            name="汇总新价格",
            returns=["new_price"],
            sql="SELECT new_price FROM updates",
        ),
        Finish(message="{q[new_price]}"),
    ])

    codes = _codes(program)
    assert "COMPUTE_VAR_UNUSED" not in codes
    assert not any(code.startswith("FOREACH_DQ") for code in codes)


def test_sql_function_like_tokens_are_not_required_foreach_fields():
    program = Program(statements=[
        ForEach(
            var="row",
            into="review_rows",
            row_fields=["Product", "Summary of Review", "Action_url"],
            body=[
                Run(
                    kind="navigation",
                    var="d",
                    name="打开 {row[Action_url]} 进入评论详情页",
                    returns=["rating"],
                    read_spec="rating：读取 Detailed Rating 数值",
                ),
            ],
        ),
        Query(
            var="q",
            name="查询低分评论",
            returns=["result"],
            sql=(
                "SELECT summary_of_review AS title, int(rating) AS rating "
                "FROM review_rows WHERE product LIKE '%Erica%' AND int(rating) <= 3"
            ),
        ),
        Finish(message="{q[result]}"),
    ])

    codes = _codes(program)
    assert "FOREACH_DQ_DETAIL_FIELD_MISSING" not in codes
    assert "FOREACH_DQ_GRID_FIELD_MISSING" not in codes


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


def _codes_of(program: Program) -> set[str]:
    return {i.code for i in validate_program(program)}


def test_no_result_source_not_tripped_by_noun_tracking_number():
    # Regression for WebArena 499 (20260708_173020): "tracking number" is a NOUN phrase; the bare
    # word "number" must not classify a pure mutation as an answer-task and force a phantom returns.
    from gui_agent.core.orchestrator.validator import _goal_expects_structured_answer

    goal = "Update order #304 with the USPS tracking number 13849373987"
    assert _goal_expects_structured_answer(goal) is False
    prog = Program(
        goal=goal,
        statements=[
            Run(name="进入订单 #304 详情页", kind="navigation", success_condition="订单详情页已显示"),
            Run(name="点 Ship 填入追踪号后 Submit Shipment", kind="action", success_condition="发货已保存，列表出现该追踪号"),
            Finish(message="已为订单 #304 添加 USPS 追踪号 13849373987。"),
        ],
    )
    assert "NO_RESULT_SOURCE" not in _codes_of(prog)


def test_no_result_source_not_tripped_by_navigation_to_filtered_list():
    from gui_agent.core.orchestrator.validator import _goal_expects_structured_answer

    goal = "Go to the list of orders that are completed"
    assert _goal_expects_structured_answer(goal) is False
    prog = Program(
        goal=goal,
        statements=[
            Run(name="进入 Sales > Orders 订单列表页", kind="navigation", success_condition="订单列表页已显示"),
            Run(name="设置 Status=Complete 并应用筛选", kind="filter", success_condition="Status=Complete 已应用"),
        ],
    )
    assert "NO_RESULT_SOURCE" not in _codes_of(prog)


def test_no_result_source_still_fires_for_number_of_count_ask():
    # But "number of X" IS a count ask — a bare finish with no result source is still rejected.
    from gui_agent.core.orchestrator.validator import _goal_expects_structured_answer

    goal = "Report the number of pending orders"
    assert _goal_expects_structured_answer(goal) is True
    prog = Program(
        goal=goal,
        statements=[
            Run(name="进入 Orders 列表并筛 Pending", kind="filter", success_condition="Status=Pending 已应用"),
            Finish(message="答案：若干。"),
        ],
    )
    assert "NO_RESULT_SOURCE" in _codes_of(prog)


def _review_drill_body_run() -> Run:
    # A per-row detail drill: opens THIS row's detail via {row[action_url]} and reads a field
    # (rating) that is absent from the grid. Its name contains the `{row[...]}` template — so the
    # `\brow\b` heuristic in _run_looks_like_table_row_field_collection matches — and it returns
    # ≥2 fields, i.e. it looks exactly like a "grid row field collection" to the text heuristic.
    return Run(
        kind="navigation",
        var="d",
        name="打开 {row[action_url]} 进入评论详情页",
        returns=["rating", "product"],
        read_spec="rating：读取 Detailed Rating 数值；product：读取产品名",
        success_condition="已进入评论详情页",
    )


def test_table_row_field_collection_exempts_drill_inside_foreach_body():
    # webarena 544 deadlock: the CORRECT plan for "aggregate a detail-only field (rating) then
    # act" must drill each review's detail inside a foreach body. That drill is the sanctioned
    # full-set collection shape, not the visible-row shortcut the rule forbids — foreach already
    # traverses every row. Before the structural exemption the rule fired on the loop-body drill
    # and no valid plan could ship. Same run at TOP LEVEL must still fire (the shortcut it guards).
    inside_foreach = Program(
        goal="统计 4 星及以上的评论数量并更新描述",  # triggers _goal_needs_table_analysis (统计/数量)
        statements=[
            ForEach(
                var="row",
                into="review_rows",
                row_fields=["action_url"],
                body=[_review_drill_body_run()],
            ),
            Query(var="q", name="统计4星以上评论", returns=["count"],
                  sql="SELECT COUNT(*) AS count FROM review_rows WHERE rating_num >= 4"),
        ],
    )
    assert "TABLE_ROW_FIELD_COLLECTION" not in _codes(inside_foreach)

    # Control: the identical row-field read as a standalone top-level step is still the forbidden
    # visible-row shortcut and must keep firing.
    top_level = Program(
        goal="统计 4 星及以上的评论数量并更新描述",
        statements=[_review_drill_body_run()],
    )
    assert "TABLE_ROW_FIELD_COLLECTION" in _codes(top_level)


def test_compute_comprehension_error_points_to_data_query():
    # webarena 544 deadlock: the model reached for a list comprehension in compute to COUNT the
    # rating>=4 rows of a foreach into table. compute is scalar-only and rejects comprehensions —
    # correct — but the bare "ListComp not allowed" message let the model just try another
    # comprehension spelling (len([...]) → [...].__len__() → len([...])) across all 3 retries and
    # never converge. The error must name the right tool so retries turn to data_query instead.
    program = Program(
        goal="统计评分 4 星及以上的评论数量",
        statements=[
            ForEach(var="row", into="review_rows", row_fields=["rating"], body=[]),
            Compute(var="count_4_plus", expr="len([x for x in review_rows if int(x['rating']) >= 4])"),
        ],
    )
    issues = validate_program(program)
    comp = [i for i in issues if i.code == "COMPUTE_UNSUPPORTED_EXPR"]
    assert comp, {i.code for i in issues}
    assert "data_query" in str(comp[0]), str(comp[0])


def test_compute_quoted_template_error_points_to_concat():
    # A compute expr is Python, not a template/f-string surface. Putting {q[count]} inside a quoted
    # string used to normalize into invalid Python and produced an unhelpful syntax error.
    program = Program(
        statements=[
            Query(var="q", name="统计", returns=["count"], sql="SELECT 3 AS count"),
            Compute(var="description", expr="'{q[count]} customer(s) love it!'"),
            Run(name="写入 {description}", kind="action"),
        ],
    )
    issues = validate_program(program)
    comp = [i for i in issues if i.code == "COMPUTE_UNSUPPORTED_EXPR"]
    assert comp, {i.code for i in issues}
    assert "str(q['count'])" in str(comp[0]), str(comp[0])


def test_compute_apostrophe_string_error_points_to_double_quotes():
    # webarena 544 retry collapsed to an otherwise valid scalar ternary, but used single quotes
    # around "don't ...", making the compute expression fail all retries.
    program = Program(
        statements=[
            Query(var="q", name="统计", returns=["count"], sql="SELECT 0 AS count"),
            Compute(
                var="description",
                expr="'don't miss out' if int(q['count']) == 0 else str(q['count']) + ' customer(s) love it!'",
            ),
            Run(name="写入 {description}", kind="action"),
        ],
    )
    issues = validate_program(program)
    comp = [i for i in issues if i.code == "COMPUTE_UNSUPPORTED_EXPR"]
    assert comp, {i.code for i in issues}
    assert "双引号" in str(comp[0]), str(comp[0])


def test_draft_bool_compute_expr_uses_python_literal():
    # Structured JSON can send expr:false as a real JSON bool. The compute dialect is Python, so the
    # lossless spelling is False; turning it into "false" creates an unknown-name error that teaches
    # the model the wrong thing.
    step = _StepDraft.model_validate({"op": "compute", "var": "x", "expr": False})
    assert step.expr == "False"


def test_lowercase_json_bool_compute_error_points_to_text_derivation():
    program = Program(
        statements=[
            Compute(var="description", expr="false"),
            Run(name="写入 {description}", kind="action"),
        ],
    )
    issues = validate_program(program)
    comp = [i for i in issues if i.code == "COMPUTE_UNSUPPORTED_EXPR"]
    assert comp, {i.code for i in issues}
    assert "JSON 字面量" in str(comp[0]), str(comp[0])
    assert "data_query" in str(comp[0]), str(comp[0])
    assert "fallback text" in str(comp[0]), str(comp[0])


def test_compute_external_sql_error_points_to_data_query():
    program = Program(
        statements=[
            Compute(
                var="count",
                expr="(q := __import__('sqlalchemy').create_engine('sqlite://').execute('SELECT 1'))",
            ),
            Run(name="使用 {count}", kind="action"),
        ],
    )
    issues = validate_program(program)
    comp = [i for i in issues if i.code == "COMPUTE_UNSUPPORTED_EXPR"]
    assert comp, {i.code for i in issues}
    assert "data_query" in str(comp[0]), str(comp[0])
    assert "外部代码" in str(comp[0]), str(comp[0])


def test_table_row_field_collection_exempts_drill_inside_function_body():
    # Review finding S6: the foreach-body exemption must also cover a drill FACTORED INTO A
    # FUNCTION (the preferred `op=call drill_fn(row)` shape). Before threading in_foreach_body into
    # the function-body walk, a factored drill returning ≥2 fields (or a marker field) tripped
    # TABLE_ROW_FIELD_COLLECTION while the identical inline foreach body was exempt — inconsistent.
    drill = FunctionDef(
        name="drill",
        params=["row"],
        returns=["product_name", "rating"],
        body=[
            Run(
                kind="navigation",
                var="d",
                name="打开 {row[action_url]} 读取该行详情",
                returns=["product_name", "rating"],
                read_spec="product_name/rating：读取详情页字段",
                success_condition="已进入详情页",
            ),
        ],
    )
    program = Program(
        goal="统计每行评分并汇总",  # triggers _goal_needs_table_analysis
        functions=[drill],
        statements=[
            ForEach(
                var="row",
                into="rows",
                row_fields=["action_url"],
                body=[Call(func="drill", args={"row": "{row}"}, var="m")],
            ),
            Query(var="q", name="汇总", returns=["n"], sql="SELECT COUNT(*) AS n FROM rows"),
        ],
    )
    assert "TABLE_ROW_FIELD_COLLECTION" not in _codes(program)


def test_consumed_mutation_result_is_allowed():
    program = Program(
        goal="更新记录并按保存结果回复",
        statements=[
            Run(
                var="save_result",
                name="更新字段并保存",
                kind="action",
                returns=["status"],
                read_spec="status：读取保存状态",
            ),
            If(
                cond=Cond(var="save_result", field="status", cmp="==", value="ok"),
                then=[Finish(message="保存成功")],
                otherwise=[Finish(message="保存失败")],
            ),
        ],
    )

    assert "MUTATION_RESULT_UNUSED" not in _codes(program)


def test_ordered_top_one_url_query_is_not_treated_as_owner_ambiguity():
    program = Program(
        statements=[
            Query(
                var="q",
                name="选出最近记录入口",
                returns=["detail_url"],
                sql=(
                    "SELECT detail_url FROM candidates "
                    "ORDER BY created_at_ts DESC LIMIT 1"
                ),
            ),
            Run(name="打开 {q[detail_url]} 进入详情页", kind="navigation"),
        ],
    )

    assert "SINGLE_TARGET_LIMIT_HIDES_AMBIGUITY" not in _codes(program)
