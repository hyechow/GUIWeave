"""Decomposer + data_query wiring for the `foreach` general-iteration primitive:
- draft (op="foreach") → AST round-trips,
- validate_program enforces in-scope {loop_var[field]} body references,
- a foreach's materialized `into` table is queryable by a following data_query (the whole point:
  collect per-item detail, then filter/aggregate the set)."""

from gui_agent.core.orchestrator import Call, ForEach, Interpreter, Program, Run, RunResult, drive
from gui_agent.core.orchestrator.data_query import execute_data_query
from gui_agent.core.orchestrator.decomposer import _FunctionDraft, _PlanDraft, _StepDraft, to_program, validate_program


def _good_draft() -> _PlanDraft:
    return _PlanDraft(goal="g", steps=[
        _StepDraft(op="run", run_kind="read", var="r", name="读候选行", returns=["id"]),
        _StepDraft(op="foreach", loop_var="row", over="r", into="reviews", body=[
            _StepDraft(op="run", run_kind="navigation", name="打开 review {row[id]} 详情"),
            _StepDraft(op="run", run_kind="read", var="d", name="读评分昵称", returns=["rating", "nickname"]),
        ]),
        _StepDraft(op="run", run_kind="data_query", var="q", name="筛 rating<=3", returns=["nickname"],
                   sql="SELECT nickname FROM reviews WHERE CAST(rating AS INTEGER) <= 3", data_scope="current"),
        _StepDraft(op="finish", message="{q[nickname]}"),
    ])


def test_foreach_draft_round_trips_and_validates_clean():
    program = to_program(_good_draft(), "g")
    # read/data_query lower to dedicated IR query nodes at construction (S6b); wire op stays "run"
    assert [type(s).__name__ for s in program.statements] == ["Read", "ForEach", "Query", "Finish"]
    fe = program.statements[1]
    assert isinstance(fe, ForEach)
    assert (fe.var, fe.over, fe.into) == ("row", "r", "reviews")
    assert [type(b).__name__ for b in fe.body] == ["Run", "Read"]
    assert validate_program(program) == []  # {row[id]} in-scope; over is a read var


def test_foreach_draft_round_trips_explicit_row_and_output_fields():
    draft = _PlanDraft(goal="g", steps=[
        _StepDraft(
            op="foreach",
            loop_var="row",
            name="采集候选行",
            row_fields=["sku", "detail_url", "current_price"],
            output_fields=["old_price", "new_price", "status"],
            into="updates",
            body_goal="处理 {row[sku]}，打开 {row[detail_url]}，按 {row[current_price]} 改价并返回结果",
        ),
    ])

    program = to_program(draft, "g")
    fe = program.statements[0]

    assert isinstance(fe, ForEach)
    assert fe.row_fields == ["sku", "detail_url", "current_price"]
    assert fe.output_fields == ["old_price", "new_price", "status"]
    assert fe.returns == []
    assert validate_program(program) == []


def test_foreach_draft_round_trips_member_desc():
    draft = _PlanDraft(goal="g", steps=[
        _StepDraft(
            op="foreach",
            loop_var="row",
            name="采集 Sahara 候选行",
            row_fields=["sku", "name", "action_url"],
            into="variant_rows",
            member_desc="size 28 的 Sahara leggings 变体",
            body=[
                _StepDraft(
                    op="run",
                    run_kind="navigation",
                    var="d",
                    name="打开 {row[action_url]} 进入变体编辑页",
                    returns=["current_price"],
                    read_spec="读取价格字段当前值",
                ),
            ],
        ),
    ])

    program = to_program(draft, "g")
    fe = program.statements[0]

    assert isinstance(fe, ForEach)
    assert fe.member_desc == "size 28 的 Sahara leggings 变体"
    assert fe.row_fields == ["sku", "name", "action_url"]
    assert [type(b).__name__ for b in fe.body] == ["Run"]
    assert validate_program(program) == []


def test_validate_accepts_over_that_is_a_read_var():
    # over no longer requires list_read=True; any read var in scope is valid
    draft = _PlanDraft(goal="g", steps=[
        _StepDraft(op="run", run_kind="read", var="r", name="读", returns=["id"]),
        _StepDraft(op="foreach", loop_var="row", over="r",
                   body=[_StepDraft(op="run", run_kind="read", var="d", name="读 {row[id]}", returns=["v"])]),
    ])
    issues = validate_program(to_program(draft, "g"))
    # No longer rejected — over=r is in scope (read step produces var="r")
    assert not any("list_read" in i for i in issues)


def test_validate_rejects_body_ref_to_unbound_var():
    # {bad[x]} in the body isn't the loop var nor a prior read → caught.
    draft = _PlanDraft(goal="g", steps=[
        _StepDraft(op="run", run_kind="read", var="r", name="读", returns=["id"]),
        _StepDraft(op="foreach", loop_var="row", over="r",
                   body=[_StepDraft(op="run", run_kind="navigation", name="打开 {bad[x]}")]),
    ])
    issues = validate_program(to_program(draft, "g"))
    assert any("bad" in i for i in issues)


def test_to_program_collapses_collection_then_enrichment_foreach():
    # Compiler pass for the 185-style malformed draft:
    # foreach #1 collects row capabilities; foreach #2 should enrich those same rows via a function.
    # Browser over="" would re-collect the grid in foreach #2, so to_program folds both into one
    # typed foreach that collects the first loop's row fields and runs the second loop's body.
    draft = _PlanDraft(goal="返回逐行详情字段",
        functions=[_FunctionDraft(name="resolve", params=["sku", "action_url"], returns=["material"], body=[
            _StepDraft(op="run", run_kind="navigation", var="d", name="打开 {action_url} 详情",
                       returns=["material"], read_spec="读 material"),
        ])],
        steps=[
            _StepDraft(op="foreach", loop_var="row", into="products_rows", returns=["sku", "action_url"]),
            _StepDraft(op="foreach", loop_var="row", into="resolved_materials", returns=["material"], body=[
                _StepDraft(
                    op="call",
                    func="resolve",
                    var="m",
                    call_args={"sku": "{row[sku]}", "action_url": "{row[action_url]}"},
                ),
            ]),
            _StepDraft(op="run", run_kind="data_query", var="q", name="汇总 material",
                       returns=["material"], sql="SELECT DISTINCT material FROM resolved_materials"),
            _StepDraft(op="finish", message="{q[material]}"),
        ])

    program = to_program(draft, "g")
    foreaches = [s for s in program.statements if isinstance(s, ForEach)]

    assert len(foreaches) == 1
    fe = foreaches[0]
    assert fe.into == "resolved_materials"
    assert fe.returns == ["sku", "action_url"]
    assert len(fe.body) == 1 and isinstance(fe.body[0], Call)
    assert fe.body[0].args == {"sku": "{row[sku]}", "action_url": "{row[action_url]}"}
    assert validate_program(program) == []


def test_validate_rejects_row_collection_direct_query_missing_detail_fields():
    draft = _PlanDraft(goal="返回详情分数<=3的负责人", steps=[
        _StepDraft(op="run", run_kind="read", var="r", name="读取候选行 id",
                   returns=["id"], read_spec="id：逐行读取，每行一个对象。"),
        _StepDraft(op="run", run_kind="data_query", var="q", name="筛详情分数<=3",
                   returns=["owner_name"],
                   sql="SELECT owner_name FROM data WHERE CAST(detail_score AS INTEGER) <= 3"),
    ])
    issues = validate_program(to_program(draft, "g"))
    assert any("不要跳过 foreach" in i for i in issues)


def test_validate_infers_row_collection_shape_from_read_spec():
    # Infers row-collection shape from read_spec keywords ("逐行", "每条记录", etc.).
    draft = _PlanDraft(goal="返回详情分数<=3的负责人", steps=[
        _StepDraft(op="run", run_kind="read", var="r", name="读取候选行 id",
                   returns=["id"], read_spec="id：逐行读取列表中每条记录的 ID，每行一个对象。"),
        _StepDraft(op="run", run_kind="data_query", var="q", name="筛详情分数<=3",
                   returns=["owner_name"],
                   sql="SELECT owner_name FROM data WHERE CAST(detail_score AS INTEGER) <= 3"),
    ])
    issues = validate_program(to_program(draft, "g"))
    assert any("不要跳过 foreach" in i for i in issues)


def test_validate_rejects_foreach_table_query_missing_body_fields():
    draft = _PlanDraft(goal="返回详情分数<=3的负责人", steps=[
        _StepDraft(op="run", run_kind="read", var="r", name="读取候选行 id",
                   returns=["id"]),
        _StepDraft(op="foreach", loop_var="row", over="r", into="detail_rows", body=[
            _StepDraft(op="run", run_kind="navigation", name="打开记录 {row[id]} 的详情"),
        ]),
        _StepDraft(op="run", run_kind="data_query", var="q", name="筛详情分数<=3",
                   returns=["owner_name"],
                   sql="SELECT owner_name FROM detail_rows WHERE CAST(detail_score AS INTEGER) <= 3"),
    ])
    issues = validate_program(to_program(draft, "g"))
    assert any("foreach body 没有通过 returns 产出的字段" in i for i in issues)


def test_validate_accepts_entity_scope_predicate_string_literal_not_a_column():
    # Regression: the entity-scope backstop (decomposer rule 11⑤) filters the drilled set by the
    # target entity, e.g. `WHERE Product LIKE '%Olivia%' AND rating <= 3`. The string literal
    # '%Olivia%' is a VALUE, not a column — the field-token extractor must strip single-quoted
    # literals before pulling identifiers, otherwise 'olivia' is mis-flagged as a phantom missing
    # detail field and the validator wrongly rejects a correct, scoped query.
    draft = _PlanDraft(goal="返回某产品评分<=3的评论者昵称", steps=[
        _StepDraft(op="run", run_kind="read", var="r", name="读取候选评论行 id 与所属产品",
                   returns=["id", "Product"]),
        _StepDraft(op="foreach", loop_var="row", over="r", into="detail_rows", body=[
            _StepDraft(op="run", run_kind="navigation", var="d", name="打开评论 {row[id]} 的详情",
                       returns=["rating", "nickname"],
                       read_spec="rating：评分；nickname：昵称"),
        ]),
        _StepDraft(op="run", run_kind="data_query", var="q", name="筛该产品评分<=3的评论者",
                   returns=["nickname"],
                   sql="SELECT nickname FROM detail_rows WHERE Product LIKE '%Olivia%' AND CAST(rating AS INTEGER) <= 3"),
    ])
    issues = validate_program(to_program(draft, "g"))
    assert not any("没有通过 returns 产出的字段" in i for i in issues), issues
    assert not any("olivia" in i.lower() for i in issues), issues


def test_validate_accepts_typed_shadow_fields_from_foreach_columns():
    draft = _PlanDraft(goal="返回最近两笔订单总金额", steps=[
        _StepDraft(
            op="foreach",
            loop_var="row",
            name="采集已完成订单行",
            returns=["Purchase Date", "Grand Total (Purchased)"],
            into="completed_orders",
            body=[],
        ),
        _StepDraft(
            op="run",
            run_kind="data_query",
            var="q",
            name="取最近两笔订单总金额",
            returns=["total"],
            sql=(
                "SELECT SUM(grand_total_purchased_num) AS total "
                "FROM (SELECT grand_total_purchased_num FROM completed_orders "
                "ORDER BY purchase_date_ts DESC LIMIT 2)"
            ),
        ),
        _StepDraft(op="finish", message="{q[total]}"),
    ])

    issues = validate_program(to_program(draft, "g"))

    assert issues == []


def test_validate_rejects_aggregate_limit_after_aggregation():
    draft = _PlanDraft(goal="返回最近四笔取消订单金额总和", steps=[
        _StepDraft(
            op="foreach",
            loop_var="row",
            name="采集取消订单行",
            returns=["Purchase Date", "Grand Total (Purchased)"],
            into="cancelled_orders",
            body=[],
        ),
        _StepDraft(
            op="run",
            run_kind="data_query",
            var="q",
            name="计算最近四笔取消订单金额",
            returns=["total"],
            sql="SELECT SUM(grand_total_purchased_num) AS total FROM cancelled_orders LIMIT 4",
        ),
    ])

    issues = validate_program(to_program(draft, "g"))

    assert any("LIMIT 放在 SUM/AVG/COUNT" in issue for issue in issues)


def test_validate_rejects_cte_aggregate_limit_after_aggregation():
    draft = _PlanDraft(goal="返回两组最近四行金额绝对差", steps=[
        _StepDraft(
            op="foreach",
            loop_var="row",
            returns=["Purchase Date", "Grand Total (Purchased)"],
            into="cancelled_orders",
            body=[],
        ),
        _StepDraft(
            op="foreach",
            loop_var="row",
            returns=["Purchase Date", "Grand Total (Purchased)"],
            into="completed_orders",
            body=[],
        ),
        _StepDraft(
            op="run",
            run_kind="data_query",
            var="q",
            name="计算差值",
            returns=["difference"],
            sql=(
                "WITH cancelled_sum AS ("
                "SELECT SUM(grand_total_purchased_num) AS total FROM cancelled_orders LIMIT 4"
                "), completed_sum AS ("
                "SELECT SUM(grand_total_purchased_num) AS total FROM completed_orders LIMIT 4"
                ") SELECT ABS((SELECT total FROM cancelled_sum) - "
                "(SELECT total FROM completed_sum)) AS difference"
            ),
        ),
    ])

    issues = validate_program(to_program(draft, "g"))

    assert any("LIMIT 放在 SUM/AVG/COUNT" in issue for issue in issues)


def test_validate_accepts_cte_abs_over_foreach_tables():
    draft = _PlanDraft(goal="返回两组最近四行金额绝对差", steps=[
        _StepDraft(
            op="foreach",
            loop_var="row",
            returns=["Purchase Date", "Grand Total (Purchased)"],
            into="cancelled_orders",
            body=[],
        ),
        _StepDraft(
            op="foreach",
            loop_var="row",
            returns=["Purchase Date", "Grand Total (Purchased)"],
            into="completed_orders",
            body=[],
        ),
        _StepDraft(
            op="run",
            run_kind="data_query",
            var="q",
            name="计算差值",
            returns=["difference"],
            sql=(
                "WITH c AS ("
                "SELECT SUM(grand_total_purchased_num) AS total_cancelled "
                "FROM (SELECT grand_total_purchased_num FROM cancelled_orders "
                "ORDER BY purchase_date_ts DESC LIMIT 4)"
                "), d AS ("
                "SELECT SUM(grand_total_purchased_num) AS total_completed "
                "FROM (SELECT grand_total_purchased_num FROM completed_orders "
                "ORDER BY purchase_date_ts DESC LIMIT 4)"
                ") SELECT ABS(total_cancelled - total_completed) AS difference FROM c, d"
            ),
        ),
        _StepDraft(op="finish", message="{q[difference]}"),
    ])

    issues = validate_program(to_program(draft, "g"))

    assert issues == []


def test_validate_accepts_derived_table_aliases_over_foreach_tables():
    draft = _PlanDraft(goal="返回两组最近四行金额绝对差", steps=[
        _StepDraft(
            op="foreach",
            loop_var="row",
            returns=["Purchase Date", "Grand Total (Purchased)"],
            into="cancelled_orders",
            body=[],
        ),
        _StepDraft(
            op="foreach",
            loop_var="row",
            returns=["Purchase Date", "Grand Total (Purchased)"],
            into="completed_orders",
            body=[],
        ),
        _StepDraft(
            op="run",
            run_kind="data_query",
            var="q",
            name="计算差值",
            returns=["difference"],
            sql=(
                "SELECT ABS(c.total - comp.total) AS difference "
                "FROM (SELECT SUM(grand_total_purchased_num) AS total "
                "FROM (SELECT grand_total_purchased_num FROM cancelled_orders "
                "ORDER BY purchase_date_ts DESC LIMIT 4)) c, "
                "(SELECT SUM(grand_total_purchased_num) AS total "
                "FROM (SELECT grand_total_purchased_num FROM completed_orders "
                "ORDER BY purchase_date_ts DESC LIMIT 4)) comp"
            ),
        ),
        _StepDraft(op="finish", message="{q[difference]}"),
    ])

    issues = validate_program(to_program(draft, "g"))

    assert issues == []


def test_validate_rejects_prior_data_query_vars_as_sql_tables():
    draft = _PlanDraft(goal="返回两组最近四行金额绝对差", steps=[
        _StepDraft(
            op="foreach",
            loop_var="row",
            returns=["Purchase Date", "Grand Total (Purchased)"],
            into="cancelled_orders",
            body=[],
        ),
        _StepDraft(
            op="run",
            run_kind="data_query",
            var="q_cancelled",
            name="计算取消订单总额",
            returns=["total_cancelled"],
            sql=(
                "SELECT SUM(grand_total_purchased_num) AS total_cancelled "
                "FROM (SELECT grand_total_purchased_num FROM cancelled_orders "
                "ORDER BY purchase_date_ts DESC LIMIT 4)"
            ),
        ),
        _StepDraft(
            op="run",
            run_kind="data_query",
            var="q",
            name="错误地把上一步 var 当表查",
            returns=["difference"],
            sql="SELECT COALESCE(total_cancelled, 0) AS difference FROM q_cancelled",
        ),
    ])

    issues = validate_program(to_program(draft, "g"))

    assert any("前序结果变量当成 SQL 表名" in issue for issue in issues)


def test_validate_rejects_temporal_limit_without_order_by():
    draft = _PlanDraft(goal="返回最近四笔订单金额", steps=[
        _StepDraft(
            op="foreach",
            loop_var="row",
            returns=["Grand Total (Purchased)"],
            into="orders",
            body=[],
        ),
        _StepDraft(
            op="run",
            run_kind="data_query",
            var="q",
            name="计算最近四笔订单金额",
            returns=["total"],
            sql=(
                "SELECT SUM(grand_total_purchased_num) AS total "
                "FROM (SELECT grand_total_purchased_num FROM orders LIMIT 4)"
            ),
        ),
    ])

    issues = validate_program(to_program(draft, "g"))

    assert any("没有 ORDER BY" in issue for issue in issues)


def test_validate_rejects_temporal_aggregate_without_limit():
    draft = _PlanDraft(goal="返回最近四笔订单金额", steps=[
        _StepDraft(
            op="foreach",
            loop_var="row",
            returns=["Purchase Date", "Grand Total (Purchased)"],
            into="orders",
            body=[],
        ),
        _StepDraft(
            op="run",
            run_kind="data_query",
            var="q",
            name="计算最近四笔订单金额",
            returns=["total"],
            sql="SELECT SUM(grand_total_purchased_num) AS total FROM orders",
        ),
    ])

    issues = validate_program(to_program(draft, "g"))

    assert any("没有先按日期/时间 ORDER BY 后 LIMIT" in issue for issue in issues)


def test_validate_body_empty_missing_shadow_field_says_add_returns_not_drill():
    draft = _PlanDraft(goal="返回最近四笔订单金额", steps=[
        _StepDraft(
            op="foreach",
            loop_var="row",
            returns=["Grand Total (Purchased)"],
            into="orders",
            body=[],
        ),
        _StepDraft(
            op="run",
            run_kind="data_query",
            var="q",
            name="计算最近四笔订单金额",
            returns=["total"],
            sql=(
                "SELECT SUM(grand_total_purchased_num) AS total "
                "FROM (SELECT grand_total_purchased_num FROM orders "
                "ORDER BY purchase_date_ts DESC LIMIT 4)"
            ),
        ),
    ])

    issues = validate_program(to_program(draft, "g"))
    message = "\n".join(issues)

    assert "加入 foreach returns" in message
    assert "打开详情" not in message


def test_validate_rejects_finish_template_expression():
    draft = _PlanDraft(goal="返回差值", steps=[
        _StepDraft(
            op="run",
            run_kind="data_query",
            var="q",
            name="读总数",
            returns=["total"],
            sql="SELECT COUNT(*) AS total FROM data",
        ),
        _StepDraft(op="finish", message="{abs(q[total] - 4)}"),
    ])

    issues = validate_program(to_program(draft, "g"))

    assert any("不支持的模板表达式" in issue for issue in issues)


def test_validate_rejects_data_query_template_refs():
    draft = _PlanDraft(goal="返回差值", steps=[
        _StepDraft(
            op="run",
            run_kind="action",
            var="c1",
            name="筛选取消订单并读取总额",
            returns=["total_canceled"],
            read_spec="total_canceled：读取页面显示的取消订单总额。",
        ),
        _StepDraft(
            op="run",
            run_kind="data_query",
            var="q",
            name="计算差值",
            returns=["difference"],
            sql="SELECT ABS({c1[total_canceled]} - 10) AS difference",
        ),
    ])

    issues = validate_program(to_program(draft, "g"))

    assert any("SQL 包含模板表达式" in issue for issue in issues)


def test_validate_rejects_visual_row_aggregation_returns():
    draft = _PlanDraft(goal="返回最近四笔订单金额差值", steps=[
        _StepDraft(
            op="run",
            run_kind="action",
            var="c",
            name="筛选取消订单并读取最近 4 笔总额",
            returns=["total_canceled"],
            read_spec="total_canceled：在订单网格中读取前 4 行 Grand Total 并手工相加得到总和。",
        ),
    ])

    issues = validate_program(to_program(draft, "g"))

    assert any("目测聚合表格前 N 行" in issue for issue in issues)


def test_validate_rejects_table_row_fields_on_filter_returns_for_aggregation_goal():
    draft = _PlanDraft(goal="返回最近四笔订单金额差值", steps=[
        _StepDraft(
            op="run",
            run_kind="filter",
            var="f",
            name="筛选取消订单并按日期降序排列",
            returns=["Purchase Date", "Grand Total (Purchased)"],
            read_spec="从当前可见的订单网格行中读取 Purchase Date 和 Grand Total (Purchased) 列的值。",
        ),
        _StepDraft(
            op="foreach",
            loop_var="row",
            returns=["Purchase Date", "Grand Total (Purchased)"],
            into="orders",
            body=[],
        ),
    ])

    issues = validate_program(to_program(draft, "g"))

    assert any("表格行字段挂在 filter returns" in issue for issue in issues)


def test_validate_rejects_post_foreach_query_missing_body_fields_without_table_ref():
    draft = _PlanDraft(goal="返回详情分数<=3的负责人", steps=[
        _StepDraft(op="run", run_kind="read", var="r", name="读取候选行 id",
                   returns=["id"]),
        _StepDraft(op="foreach", loop_var="row", over="r", into="detail_rows", body=[
            _StepDraft(op="run", run_kind="navigation", name="打开记录 {row[id]} 的详情"),
        ]),
        _StepDraft(op="run", run_kind="data_query", var="q", name="筛详情分数<=3",
                   returns=["owner_name"],
                   sql="SELECT owner_name FROM data WHERE CAST(detail_score AS INTEGER) <= 3"),
    ])
    issues = validate_program(to_program(draft, "g"))
    assert any("位于 foreach 之后" in i for i in issues)


def test_nested_foreach_in_body_is_dropped():
    # One level only: a nested foreach in the body is stripped at conversion (deterministic backstop).
    draft = _PlanDraft(goal="g", steps=[
        _StepDraft(op="run", run_kind="read", var="r", name="读", returns=["id"]),
        _StepDraft(op="foreach", loop_var="row", over="r", body=[
            _StepDraft(op="run", run_kind="navigation", name="打开 {row[id]}"),
            _StepDraft(op="foreach", loop_var="inner", over="r", body=[]),  # nested → dropped
        ]),
    ])
    program = to_program(draft, "g")
    fe = program.statements[1]
    assert all(type(b).__name__ != "ForEach" for b in fe.body)


def test_data_query_runs_on_foreach_materialized_table():
    """End-to-end of the accumulate→query seam (deterministic): run the program's foreach with a mock
    executor, then run the data_query SQL against the interpreter's materialized table."""
    program = to_program(_good_draft(), "g")
    details = {"1": {"rating": "5", "nickname": "Jane"},
               "2": {"rating": "2", "nickname": "Emma"},
               "3": {"rating": "3", "nickname": "Seam"}}
    last_open: list[str] = []

    def execute(run: Run) -> RunResult:
        if run.name == "读候选行" and run.kind == "read":
            return RunResult(completed=True, rows=[{"id": "1"}, {"id": "2"}, {"id": "3"}])
        if run.name.startswith("打开"):
            last_open.append(run.name.split("review ", 1)[1].split(" ", 1)[0])
            return RunResult(completed=True)
        if run.kind == "read":
            return RunResult(completed=True, reads=details[last_open[-1]])
        if run.kind == "data_query":
            return RunResult(completed=True)  # the live loop runs SQL; here we assert it separately
        return RunResult(completed=True)

    interp = Interpreter(program)
    drive(interp, execute)
    snapshots = interp.materialized_tables()  # what the live data_query path folds into its source
    out = execute_data_query(
        snapshots, "SELECT nickname FROM reviews WHERE CAST(rating AS INTEGER) <= 3", ["nickname"],
        require_complete=False,
    )
    # rating<=3 → Emma(2) + Seam(3), not Jane(5)
    assert "Emma" in str(out) and "Seam" in str(out) and "Jane" not in str(out)
