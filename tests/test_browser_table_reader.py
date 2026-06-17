from __future__ import annotations

from gui_agent.adapters.browser.table_reader import (
    normalize_table_snapshots,
    table_snapshot_js,
)
from gui_agent.core.schemas import Observation


def test_table_snapshot_js_is_serialized_expression():
    js = table_snapshot_js()
    assert "JSON.stringify" in js
    assert "role=\"grid\"" in js
    assert "records?" in js


def test_normalize_table_snapshots_maps_rows_to_headers():
    raw = {
        "url": "http://example.test/admin/orders",
        "title": "Orders",
        "tables": [
            {
                "source": "table",
                "caption": "Orders",
                "headers": ["Customer Email", "Status", "Status"],
                "rows": [
                    ["a@example.com", "complete", "paid"],
                    ["b@example.com", "canceled", "void"],
                ],
                "domRows": 2,
                "totalRecords": "308",
            }
        ],
    }

    tables = normalize_table_snapshots(raw)

    assert len(tables) == 1
    assert tables[0]["partial"] is True
    assert tables[0]["headers"] == ["Customer Email", "Status", "Status_2"]
    assert tables[0]["rows"][0] == {
        "Customer Email": "a@example.com",
        "Status": "complete",
        "Status_2": "paid",
    }
    assert tables[0]["page"]["title"] == "Orders"


def test_observation_accepts_optional_table_snapshots():
    obs = Observation(png_bytes=b"png", source="browser", tables=[{"rows": []}])
    assert obs.tables == [{"rows": []}]
