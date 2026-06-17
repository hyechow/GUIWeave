from __future__ import annotations

import pytest

from gui_agent.core.orchestrator import Finish, Interpreter, Program, Run
from gui_agent.core.orchestrator.data_query import DataQueryError, execute_data_query
from gui_agent.core.run.non_ui import drive_pending_non_ui
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


def test_execute_data_query_rejects_mutating_sql():
    with pytest.raises(DataQueryError, match="只允许 SELECT|禁止关键字"):
        execute_data_query(_orders_table(), "DROP TABLE data", ["result"])


def test_execute_data_query_rejects_partial_tables_by_default():
    with pytest.raises(DataQueryError, match="表格快照不完整"):
        execute_data_query(_orders_table(partial=True), "SELECT COUNT(*) AS n FROM data", ["n"])


def test_non_ui_complete_data_query_uses_platform_complete_tables(tmp_path):
    class _Platform:
        def __init__(self) -> None:
            self.called = False

        def read_complete_tables(self):
            self.called = True
            return _orders_table(partial=False)

    prog = Program(
        statements=[
            Run(
                var="q",
                name="查询邮箱",
                kind="data_query",
                returns=["emails"],
                sql="SELECT customer_email FROM data WHERE lower(status) = 'complete' ORDER BY customer_email",
            ),
            Finish(message="{q[emails]}"),
        ]
    )
    interp = Interpreter(prog)
    steps = interp.steps()
    current_run = next(steps)
    platform = _Platform()
    context = PolicyContext(
        goal="goal",
        supervisor_policy_name="milestone",
        action_policy_name="action",
    )

    result = drive_pending_non_ui(
        current_run=current_run,
        run_index=0,
        notes_mark=0,
        interpreter_steps=steps,
        bundle=None,
        platform=platform,
        log_dir=tmp_path,
        supervisor=None,
        context=context,
        save_context=lambda: None,
        say=lambda _msg: None,
        done_observation=Observation(
            png_bytes=b"png",
            source="browser",
            tables=_orders_table(partial=True),
        ),
        observation_url="screenshot_turn_1.png",
    )

    assert platform.called is True
    assert result.current_run is None
    assert result.reply == '["a@example.com", "b@example.com", "c@example.com"]'
    assert context.turns[-1].executed is True
    assert context.turns[-1].non_ui["reads"] == {
        "emails": '["a@example.com", "b@example.com", "c@example.com"]'
    }
