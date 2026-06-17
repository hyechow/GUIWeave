from __future__ import annotations

import pytest

from gui_agent.core.orchestrator.data_query import DataQueryError, execute_data_query


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


def test_execute_data_query_rejects_mutating_sql():
    with pytest.raises(DataQueryError, match="只允许 SELECT|禁止关键字"):
        execute_data_query(_orders_table(), "DROP TABLE data", ["result"])


def test_execute_data_query_rejects_partial_tables_by_default():
    with pytest.raises(DataQueryError, match="表格快照不完整"):
        execute_data_query(_orders_table(partial=True), "SELECT COUNT(*) AS n FROM data", ["n"])
