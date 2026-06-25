from __future__ import annotations

from types import SimpleNamespace

import pytest

from gui_agent.core.orchestrator import Finish, Interpreter, Program, Run
from gui_agent.core.orchestrator.data_query import DataQueryError, execute_data_query
from gui_agent.core.run.non_interactive import drive_pending_non_ui
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


def test_execute_data_query_rejects_partial_tables_by_default():
    with pytest.raises(DataQueryError, match="表格快照不完整"):
        execute_data_query(_orders_table(partial=True), "SELECT COUNT(*) AS n FROM data", ["n"])


def test_non_ui_repairs_empty_data_query_with_actual_table_snapshot(tmp_path, monkeypatch):
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
        "gui_agent.core.orchestrator.data_query_repair.repair_data_query_sql",
        _fake_repair,
    )

    prog = Program(
        statements=[
            Run(
                var="q",
                name="统计月度订单数",
                kind="data_query",
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
    interp = Interpreter(prog)
    steps = interp.steps()
    current_run = next(steps)
    context = PolicyContext(
        goal="Get monthly count of completed orders",
        supervisor_policy_name="milestone",
        action_policy_name="action",
    )

    result = drive_pending_non_ui(
        current_run=current_run,
        run_index=0,
        notes_mark=0,
        interpreter_steps=steps,
        bundle=None,
        platform=_Platform(),
        log_dir=tmp_path,
        supervisor=None,
        context=context,
        save_context=lambda: None,
        say=lambda _msg: None,
        done_observation=Observation(
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


def test_non_ui_repair_blocks_when_collected_source_conflicts_with_goal(tmp_path, monkeypatch):
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
        "gui_agent.core.orchestrator.data_query_repair.repair_data_query_sql",
        _fake_repair,
    )

    prog = Program(
        statements=[
            Run(
                var="q",
                name="统计全量历史记录",
                kind="data_query",
                returns=["result"],
                sql="SELECT missing_column FROM data",
            ),
            Finish(message="{q[result]}"),
        ]
    )
    interp = Interpreter(prog)
    steps = interp.steps()
    current_run = next(steps)
    context = PolicyContext(
        goal="Get the ranking across the entire history",
        supervisor_policy_name="milestone",
        action_policy_name="action",
    )

    result = drive_pending_non_ui(
        current_run=current_run,
        run_index=0,
        notes_mark=0,
        interpreter_steps=steps,
        bundle=None,
        platform=_Platform(),
        log_dir=tmp_path,
        supervisor=None,
        context=context,
        save_context=lambda: None,
        say=lambda _msg: None,
        done_observation=Observation(
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


def test_non_ui_repair_empty_result_after_sql_error_still_fails(tmp_path, monkeypatch):
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
        "gui_agent.core.orchestrator.data_query_repair.repair_data_query_sql",
        _fake_repair,
    )

    prog = Program(
        statements=[
            Run(
                var="q",
                name="查询匹配记录",
                kind="data_query",
                returns=["result"],
                sql="SELECT missing_column FROM data",
            ),
            Finish(message="{q[result]}"),
        ]
    )
    interp = Interpreter(prog)
    steps = interp.steps()
    current_run = next(steps)
    context = PolicyContext(
        goal="Get matching records",
        supervisor_policy_name="milestone",
        action_policy_name="action",
    )

    result = drive_pending_non_ui(
        current_run=current_run,
        run_index=0,
        notes_mark=0,
        interpreter_steps=steps,
        bundle=None,
        platform=_Platform(),
        log_dir=tmp_path,
        supervisor=None,
        context=context,
        save_context=lambda: None,
        say=lambda _msg: None,
        done_observation=Observation(
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


def test_non_ui_data_query_failure_sets_failure_evidence(tmp_path, monkeypatch):
    # Feasibility Guard non-UI kick-back: a data_query that fails carries failure_evidence so the loop can
    # re-decompose instead of ending the run.
    import gui_agent.core.orchestrator.data_query as dq

    def _raise(*_a, **_k):
        raise dq.DataQueryError("SQL 引用了不存在的列 rating")

    monkeypatch.setattr(dq, "execute_data_query", _raise)
    prog = Program(statements=[
        Run(var="q", name="查询评分", kind="data_query", returns=["x"],
            sql="SELECT rating FROM data", data_scope="current"),
        Finish(message="{q[x]}"),
    ])
    steps = Interpreter(prog).steps()
    cur = next(steps)
    ctx = PolicyContext(goal="g", supervisor_policy_name="milestone", action_policy_name="action")
    result = drive_pending_non_ui(
        current_run=cur, run_index=0, notes_mark=0, interpreter_steps=steps,
        bundle=None, platform=None, log_dir=tmp_path, supervisor=None, context=ctx,
        save_context=lambda: None, say=lambda _m: None,
        done_observation=Observation(png_bytes=b"png", source="browser", tables=[]),
        observation_url="x.png",
    )
    assert result.current_run is None           # program ended on the failure
    assert result.failure_evidence is not None   # ... but carries re-plannable evidence
    assert "rating" in result.failure_evidence
