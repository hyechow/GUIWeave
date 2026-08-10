from __future__ import annotations

import json
from pathlib import Path

import pytest

from gui_agent.core.tool_agent.contracts import DataRequirement
from gui_agent.core.tool_agent.perception import (
    DataNormalizationError,
    _structured_rows,
)
from gui_agent.core.tool_agent.sandbox import execute_transform


_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "tool_agent"
    / "task108_completed_orders_typed_rows.json"
)

_RESULT_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "month": {"type": "string"},
            "count": {"type": "integer"},
        },
        "required": ["month", "count"],
    },
}

_CANONICAL_TRANSFORM = '''
def transform(inputs):
    rows = inputs[0]
    month_names = ["January", "February", "March", "April", "May"]
    counts = {month: 0 for month in month_names}
    for row in rows:
        month_index = int(row["purchase_date"][5:7]) - 1
        if 0 <= month_index < len(month_names):
            counts[month_names[month_index]] += 1
    return [{"month": month, "count": counts[month]} for month in month_names]
'''


def _requirement() -> DataRequirement:
    return DataRequirement(
        id="completed_orders",
        description="Completed orders from January through May 2023",
        row_schema={
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "status": {"type": "string"},
                "purchase_date": {"type": "string"},
            },
            "required": ["id", "status", "purchase_date"],
        },
        field_sources={
            "id": "ID",
            "status": "Status",
            "purchase_date": "Purchase Date",
        },
        field_types={
            "id": "text",
            "status": "text",
            "purchase_date": "datetime",
        },
    )


def test_task108_rows_normalize_before_frozen_transform() -> None:
    case = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    requirement = _requirement()

    rows = _structured_rows(requirement, {"rows": case["source_rows"]})
    result = execute_transform(_CANONICAL_TRANSFORM, [rows], _RESULT_SCHEMA)

    assert len(rows) == 38
    assert rows[0]["purchase_date"] == "2023-05-19T08:11:51+00:00"
    assert result == case["expected"]
    assert sum(item["count"] for item in result) == len(rows)


def test_declared_datetime_rejects_unparseable_source_value() -> None:
    with pytest.raises(
        DataNormalizationError,
        match="field 'purchase_date' cannot normalize as datetime",
    ):
        _structured_rows(
            _requirement(),
            {"rows": [{
                "ID": "1",
                "Status": "Complete",
                "Purchase Date": "not a date",
            }]},
        )
