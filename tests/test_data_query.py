from __future__ import annotations

from types import SimpleNamespace

import pytest

from gui_agent.core.orchestrator import Finish, Program, Query, Run
from gui_agent.core.orchestrator.primitives.data_query import DataQueryError, execute_data_query
from gui_agent.core.run.program_runtime import ProgramRuntime
from gui_agent.core.run.statements import drain_immediate_statements
from gui_agent.core.schemas import Observation, PolicyContext


def _orders_table(*, partial: bool = False):
    return [
        {
            "index": 1,
            "caption": "Orders",
            "headers": ["Customer Email", "Status"],
            "rows": [
                {"Customer Email": "a@example.com", "Status": "complete"},
                {"Customer Email": "a@example.com", "Status": "closed"},
                {"Customer Email": "b@example.com", "Status": "complete"},
                {"Customer Email": "b@example.com", "Status": "canceled"},
                {"Customer Email": "c@example.com", "Status": "complete"},
            ],
            "row_count": 5,
            "total_records": 10 if partial else 5,
            "partial": partial,
        }
    ]


def test_execute_data_query_group_by_with_sanitized_columns():
    reads = execute_data_query(
        _orders_table(),
        """
        SELECT customer_email
        FROM data
        GROUP BY customer_email
        HAVING COUNT(*) = 2
        ORDER BY customer_email
        """,
        ["emails"],
    )

    assert reads == {"emails": '["a@example.com", "b@example.com"]'}


def test_execute_data_query_rewrites_quoted_source_labels():
    reads = execute_data_query(
        _orders_table(),
        """
        WITH order_counts AS (
          SELECT "Customer Email" AS customer_email, COUNT(*) AS cnt
          FROM data
          WHERE "Status" != 'missing'
          GROUP BY "Customer Email"
        ),
        ranked AS (
          SELECT customer_email, cnt, DENSE_RANK() OVER (ORDER BY cnt DESC) AS rnk
          FROM order_counts
        )
        SELECT customer_email
        FROM ranked
        WHERE rnk = 2
        ORDER BY customer_email
        """,
        ["second_most"],
    )

    assert reads == {"second_most": "c@example.com"}


def test_execute_data_query_supports_with_select():
    reads = execute_data_query(
        _orders_table(),
        """
        WITH counts AS (
          SELECT customer_email, COUNT(*) AS cnt
          FROM orders
          WHERE lower(status) = 'complete'
          GROUP BY customer_email
        )
        SELECT customer_email
        FROM counts
        WHERE cnt = 1
        ORDER BY customer_email
        """,
        ["complete_once"],
    )

    assert reads == {"complete_once": '["a@example.com", "b@example.com", "c@example.com"]'}


def test_execute_data_query_supports_window_functions_for_ranked_counts():
    reads = execute_data_query(
        _orders_table(),
        """
        WITH counts AS (
          SELECT customer_email, COUNT(*) AS cnt
          FROM data
          GROUP BY customer_email
        ),
        ranked AS (
          SELECT customer_email, cnt, DENSE_RANK() OVER (ORDER BY cnt DESC) AS rnk
          FROM counts
        )
        SELECT customer_email
        FROM ranked
        WHERE rnk = 2
        ORDER BY customer_email
        """,
        ["second_most"],
    )

    assert reads == {"second_most": "c@example.com"}


def test_execute_data_query_exposes_caption_alias():
    reads = execute_data_query(
        [
            {
                "index": 1,
                "caption": "Top Search Terms",
                "headers": ["Search Term", "Results", "Uses"],
                "rows": [
                    {"Search Term": "hollister", "Results": "1", "Uses": "19"},
                    {"Search Term": "Joust Bag", "Results": "10", "Uses": "4"},
                    {"Search Term": "tanks", "Results": "23", "Uses": "1"},
                ],
                "row_count": 3,
                "partial": False,
            }
        ],
        """
        SELECT search_term
        FROM top_search_terms
        ORDER BY CAST(uses AS INTEGER) DESC
        LIMIT 2
        """,
        ["top_terms"],
    )

    assert reads == {"top_terms": '["hollister", "Joust Bag"]'}


def test_execute_data_query_returns_object_rows_as_single_result():
    reads = execute_data_query(
        [
            {
                "index": 1,
                "caption": "Orders",
                "headers": ["created_at", "status"],
                "rows": [
                    {"created_at": "2023-01-03 12:00:00", "status": "complete"},
                    {"created_at": "2023-01-17 09:00:00", "status": "complete"},
                    {"created_at": "2023-02-01 08:00:00", "status": "complete"},
                ],
                "row_count": 3,
                "partial": False,
            }
        ],
        """
        SELECT
          CASE strftime('%m', created_at)
            WHEN '01' THEN 'January'
            WHEN '02' THEN 'February'
          END AS month,
          COUNT(*) AS count
        FROM data
        GROUP BY strftime('%m', created_at)
        ORDER BY strftime('%m', created_at)
        """,
        ["result"],
    )

    assert reads == {
        "result": '[{"month": "January", "count": 2}, {"month": "February", "count": 1}]'
    }


def test_execute_data_query_rejects_missing_multi_return_aliases():
    with pytest.raises(DataQueryError, match="缺少 returns 字段: month, count"):
        execute_data_query(
            _orders_table(),
            """
            SELECT status AS month_key, COUNT(*) AS cnt
            FROM data
            GROUP BY status
            """,
            ["month", "count"],
        )


def test_execute_data_query_rejects_mutating_sql():
    with pytest.raises(DataQueryError, match="只允许 SELECT|禁止关键字"):
        execute_data_query(_orders_table(), "DROP TABLE data", ["result"])


def test_execute_data_query_rejects_template_refs():
    with pytest.raises(DataQueryError, match="不支持模板表达式"):
        execute_data_query(
            _orders_table(),
            "SELECT ABS({c1[total_canceled]} - 10) AS difference",
            ["difference"],
        )


def test_execute_data_query_rejects_partial_tables_by_default():
    with pytest.raises(DataQueryError, match="表格快照不完整"):
        execute_data_query(_orders_table(partial=True), "SELECT COUNT(*) AS n FROM data", ["n"])


def test_execute_data_query_exposes_typed_shadows_for_currency_and_dates():
    reads = execute_data_query(
        [
            {
                "caption": "completed_orders",
                "headers": ["Purchase Date", "Grand Total (Purchased)"],
                "rows": [
                    {"Purchase Date": "Feb 3, 2023 6:08:03 PM", "Grand Total (Purchased)": "$106.00"},
                    {"Purchase Date": "May 7, 2023 6:41:05 PM", "Grand Total (Purchased)": "$90.00"},
                    {"Purchase Date": "Jun 1, 2023 1:00:00 PM", "Grand Total (Purchased)": "$92.40"},
                ],
                "partial": False,
            }
        ],
        """
        SELECT SUM(grand_total_purchased_num) AS total
        FROM (
          SELECT grand_total_purchased_num
          FROM completed_orders
          ORDER BY purchase_date_ts DESC
          LIMIT 2
        )
        """,
        ["total"],
    )

    assert reads == {"total": "182.4"}


def test_execute_data_query_rejects_aggregate_limit_after_aggregation():
    with pytest.raises(DataQueryError, match="LIMIT 放在聚合之后"):
        execute_data_query(
            [
                {
                    "caption": "cancelled_orders",
                    "headers": ["Purchase Date", "Grand Total (Purchased)"],
                    "rows": [
                        {"Purchase Date": "Feb 3, 2023 6:08:03 PM", "Grand Total (Purchased)": "$106.00"},
                        {"Purchase Date": "May 7, 2023 6:41:05 PM", "Grand Total (Purchased)": "$90.00"},
                    ],
                    "partial": False,
                }
            ],
            "SELECT SUM(grand_total_purchased_num) AS total FROM cancelled_orders LIMIT 4",
            ["total"],
        )


def test_execute_data_query_rejects_cte_aggregate_limit_after_aggregation():
    with pytest.raises(DataQueryError, match="LIMIT 放在聚合之后"):
        execute_data_query(
            [
                {
                    "caption": "cancelled_orders",
                    "headers": ["Purchase Date", "Grand Total (Purchased)"],
                    "rows": [
                        {"Purchase Date": "Feb 3, 2023 6:08:03 PM", "Grand Total (Purchased)": "$106.00"},
                        {"Purchase Date": "May 7, 2023 6:41:05 PM", "Grand Total (Purchased)": "$90.00"},
                    ],
                    "partial": False,
                },
                {
                    "caption": "completed_orders",
                    "headers": ["Purchase Date", "Grand Total (Purchased)"],
                    "rows": [
                        {"Purchase Date": "Feb 4, 2023 6:08:03 PM", "Grand Total (Purchased)": "$100.00"},
                    ],
                    "partial": False,
                },
            ],
            """
            WITH cancelled_sum AS (
              SELECT SUM(grand_total_purchased_num) AS total
              FROM cancelled_orders
              LIMIT 4
            ),
            completed_sum AS (
              SELECT SUM(grand_total_purchased_num) AS total
              FROM completed_orders
              LIMIT 4
            )
            SELECT ABS((SELECT total FROM cancelled_sum) - (SELECT total FROM completed_sum)) AS difference
            """,
            ["difference"],
        )


def test_execute_data_query_allows_complete_materialized_table_with_partial_dom_sibling():
    reads = execute_data_query(
        [
            {
                "caption": "completed_orders",
                "headers": ["Grand Total (Purchased)"],
                "rows": [
                    {"Grand Total (Purchased)": "$106.00"},
                    {"Grand Total (Purchased)": "$159.40"},
                ],
                "partial": False,
            },
            {
                "caption": "Orders",
                "headers": ["ID", "Grand Total (Purchased)", "Status"],
                "rows": [
                    {"ID": "000000004", "Grand Total (Purchased)": "$106.00", "Status": "Complete"},
                ],
                "row_count": 1,
                "total_records": 153,
                "partial": True,
                "path": "div#container>div.admin__data-grid-outer-wrap>div.admin__data-grid-wrap>table.data-grid.data-grid-draggable",
            },
        ],
        """
        SELECT SUM(grand_total_purchased_num) AS total
        FROM (SELECT grand_total_purchased_num FROM completed_orders LIMIT 2)
        """,
        ["total"],
    )

    assert reads == {"total": "265.4"}


def test_query_executor_repairs_empty_result_with_actual_table_snapshot(tmp_path, monkeypatch):
    table = [
        {
            "index": 1,
            "caption": "sales_order_grid",
            "headers": ["created_at", "status"],
            "rows": [
                {"created_at": "2023-01-03 12:00:00", "status": "complete"},
                {"created_at": "2023-01-17 09:00:00", "status": "complete"},
                {"created_at": "2023-02-01 08:00:00", "status": "complete"},
            ],
            "row_count": 3,
            "partial": False,
        }
    ]

    class _Platform:
        pass

    def _fake_repair(**kwargs):
        assert "非空表格上返回空结果" in kwargs["failure"]
        assert kwargs["requested_returns"] == ["result"]
        return SimpleNamespace(
            reason="use already-filtered rows and project month names",
            sql="""
            SELECT
              CASE strftime('%m', created_at)
                WHEN '01' THEN 'January'
                WHEN '02' THEN 'February'
              END AS month,
              COUNT(*) AS count
            FROM data
            GROUP BY strftime('%m', created_at)
            ORDER BY strftime('%m', created_at)
            """,
        )

    monkeypatch.setattr(
        "gui_agent.core.orchestrator.primitives.data_query_repair.repair_data_query_sql",
        _fake_repair,
    )

    prog = Program(
        statements=[
            Query(
                var="q",
                name="统计月度订单数", 
                returns=["result"],
                sql="""
                SELECT strftime('%m', created_at) AS month_num, COUNT(*) AS count
                FROM data
                WHERE status = 'Complete'
                GROUP BY strftime('%m', created_at)
                ORDER BY month_num
                """,
            ),
            Finish(message="{q[result]}"),
        ]
    )
    runtime = ProgramRuntime.start(prog)
    context = PolicyContext(
        goal="Get monthly count of completed orders",
        supervisor_policy_name="milestone",
        action_policy_name="action",
    )

    result = drain_immediate_statements(
        program_runtime=runtime,
        bundle=None,
        platform=_Platform(),
        log_dir=tmp_path,
        check_knowledge="",
        context=context,
        save_context=lambda: None,
        say=lambda _msg: None,
        observation=Observation(
            png_bytes=b"png",
            source="browser",
            tables=table,
        ),
        observation_url="screenshot_turn_1.png",
    )

    assert result.reply == '[{"month": "January", "count": 2}, {"month": "February", "count": 1}]'
    assert context.turns[-1].executed is True
    assert "status = 'Complete'" not in context.turns[-1].non_ui["sql"]
    assert context.turns[-1].non_ui["reads"] == {
        "result": '[{"month": "January", "count": 2}, {"month": "February", "count": 1}]'
    }


def test_query_executor_blocks_when_collected_source_conflicts_with_goal(tmp_path, monkeypatch):
    table = [
        {
            "index": 1,
            "caption": "Records",
            "headers": ["owner", "count"],
            "rows": [{"owner": "a@example.com", "count": "1"}],
            "row_count": 1,
            "partial": False,
        }
    ]

    class _Platform:
        pass

    def _fake_repair(**_kwargs):
        return SimpleNamespace(
            source_ok=False,
            source_issue="当前表格仍是未请求的日期范围筛选结果，需要回到界面清除该筛选",
            reason="wrong source scope",
            sql="",
        )

    monkeypatch.setattr(
        "gui_agent.core.orchestrator.primitives.data_query_repair.repair_data_query_sql",
        _fake_repair,
    )

    prog = Program(
        statements=[
            Query(
                var="q",
                name="统计全量历史记录", 
                returns=["result"],
                sql="SELECT missing_column FROM data",
            ),
            Finish(message="{q[result]}"),
        ]
    )
    runtime = ProgramRuntime.start(prog)
    context = PolicyContext(
        goal="Get the ranking across the entire history",
        supervisor_policy_name="milestone",
        action_policy_name="action",
    )

    result = drain_immediate_statements(
        program_runtime=runtime,
        bundle=None,
        platform=_Platform(),
        log_dir=tmp_path,
        check_knowledge="",
        context=context,
        save_context=lambda: None,
        say=lambda _msg: None,
        observation=Observation(
            png_bytes=b"png",
            source="browser",
            tables=table,
        ),
        observation_url="screenshot_turn_1.png",
    )

    assert result.reply is not None
    assert "数据源与任务意图不一致" in result.reply
    assert "清除该筛选" in result.reply
    assert context.turns[-1].executed is False
    assert context.turns[-1].non_ui["failed"] is True
    assert context.turns[-1].non_ui["reads"] == {}


def test_query_executor_empty_repair_after_sql_error_still_fails(tmp_path, monkeypatch):
    table = [
        {
            "index": 1,
            "caption": "Records",
            "headers": ["owner"],
            "rows": [{"owner": "a@example.com"}],
            "row_count": 1,
            "partial": False,
        }
    ]

    class _Platform:
        pass

    def _fake_repair(**_kwargs):
        return SimpleNamespace(
            source_ok=True,
            reason="repair column name but no matching rows",
            sql="SELECT owner AS result FROM data WHERE owner = 'nobody@example.com'",
        )

    monkeypatch.setattr(
        "gui_agent.core.orchestrator.primitives.data_query_repair.repair_data_query_sql",
        _fake_repair,
    )

    prog = Program(
        statements=[
            Query(
                var="q",
                name="查询匹配记录", 
                returns=["result"],
                sql="SELECT missing_column FROM data",
            ),
            Finish(message="{q[result]}"),
        ]
    )
    runtime = ProgramRuntime.start(prog)
    context = PolicyContext(
        goal="Get matching records",
        supervisor_policy_name="milestone",
        action_policy_name="action",
    )

    result = drain_immediate_statements(
        program_runtime=runtime,
        bundle=None,
        platform=_Platform(),
        log_dir=tmp_path,
        check_knowledge="",
        context=context,
        save_context=lambda: None,
        say=lambda _msg: None,
        observation=Observation(
            png_bytes=b"png",
            source="browser",
            tables=table,
        ),
        observation_url="screenshot_turn_1.png",
    )

    assert result.reply is not None
    assert "SQL 修复后仍返回空结果" in result.reply
    assert context.turns[-1].executed is False
    assert context.turns[-1].non_ui["failed"] is True


def test_query_failure_sets_replan_evidence(tmp_path, monkeypatch):
    # A failed query carries evidence so Program runtime can recompile instead of silently ending.
    # re-decompose instead of ending the run.
    import gui_agent.core.orchestrator.primitives.data_query as dq

    def _raise(*_a, **_k):
        raise dq.DataQueryError("SQL 引用了不存在的列 rating")

    monkeypatch.setattr(dq, "execute_data_query", _raise)
    prog = Program(statements=[
        Query(var="q", name="查询评分",  returns=["x"],
            sql="SELECT rating FROM data", data_scope="current"),
        Finish(message="{q[x]}"),
    ])
    runtime = ProgramRuntime.start(prog)
    ctx = PolicyContext(goal="g", supervisor_policy_name="milestone", action_policy_name="action")
    result = drain_immediate_statements(
        program_runtime=runtime,
        bundle=None, platform=None, log_dir=tmp_path, check_knowledge="", context=ctx,
        save_context=lambda: None, say=lambda _m: None,
        observation=Observation(png_bytes=b"png", source="browser", tables=[]),
        observation_url="x.png",
    )
    assert runtime.current is None              # program ended on the failure
    assert result.failure_evidence is not None   # ... but carries re-plannable evidence
    assert "rating" in result.failure_evidence
