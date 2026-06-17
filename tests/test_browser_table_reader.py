from __future__ import annotations

from gui_agent.adapters.browser.table_reader import (
    complete_table_snapshot_js,
    normalize_table_snapshots,
    table_snapshot_js,
)
from gui_agent.core.schemas import Observation


def test_table_snapshot_js_is_serialized_expression():
    js = table_snapshot_js()
    assert "JSON.stringify" in js
    assert "role=\"grid\"" in js
    assert "records?" in js
    assert ".dashboard-item-title" in js
    assert "aria-labelledby" in js


def test_complete_table_snapshot_js_fetches_magento_mui_pages():
    js = complete_table_snapshot_js()
    assert "/mui/index/render/" in js
    assert "paging[pageSize]" in js
    assert "totalRecords" in js
    assert "credentials: \"same-origin\"" in js


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


def test_normalize_table_snapshots_accepts_provider_dict_rows():
    raw = {
        "url": "http://example.test/admin/orders",
        "title": "Orders",
        "tables": [
            {
                "source": "magento-mui",
                "caption": "sales_order_grid",
                "headers": ["increment_id", "customer_email", "status"],
                "rows": [
                    {
                        "increment_id": "000000001",
                        "customer_email": "a@example.com",
                        "status": "complete",
                    },
                    {
                        "increment_id": "000000002",
                        "customer_email": "b@example.com",
                        "status": "processing",
                    },
                ],
                "domRows": 2,
                "totalRecords": 2,
            }
        ],
    }

    tables = normalize_table_snapshots(raw)

    assert len(tables) == 1
    assert tables[0]["source"] == "magento-mui"
    assert tables[0]["partial"] is False
    assert tables[0]["headers"] == ["increment_id", "customer_email", "status"]
    assert tables[0]["rows"][0]["customer_email"] == "a@example.com"


def test_observation_accepts_optional_table_snapshots():
    obs = Observation(png_bytes=b"png", source="browser", tables=[{"rows": []}])
    assert obs.tables == [{"rows": []}]
