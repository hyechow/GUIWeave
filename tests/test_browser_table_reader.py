from __future__ import annotations

from gui_agent.adapters.browser.table_reader import (
    normalize_table_snapshots,
    normalize_viewport,
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
    assert "current-page-input" in js
    assert "detectPageViewport" in js
    assert "viewport: detectPageViewport" in js
    assert "document.documentElement.scrollHeight" in js


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


# ── traversal state: pager/scroll detection from the DOM ───────────────────────────

def test_normalize_preserves_traversal_paged_with_page_index():
    raw = {
        "url": "http://example.test/admin/reviews",
        "title": "Reviews",
        "tables": [{
            "source": "table",
            "caption": "Product Reviews",
            "headers": ["ID", "Nickname"],
            "rows": [{"ID": "1", "Nickname": "Amy"}, {"ID": "2", "Nickname": "Bob"}],
            "totalRecords": "27",
            "traversal": {
                "type": "paged",
                "page_index": 1,
                "page_count": 2,
                "has_next_page": True,
                "has_prev_page": False,
            },
        }],
    }
    tables = normalize_table_snapshots(raw)
    assert len(tables) == 1
    assert tables[0]["traversal"]["type"] == "paged"
    assert tables[0]["traversal"]["page_index"] == 1
    assert tables[0]["traversal"]["page_count"] == 2
    assert tables[0]["traversal"]["has_next_page"] is True
    assert tables[0]["traversal"]["has_prev_page"] is False


def test_normalize_preserves_traversal_on_last_page():
    raw = {
        "url": "http://example.test/admin/reviews",
        "title": "Reviews",
        "tables": [{
            "source": "table",
            "headers": ["ID", "Nickname"],
            "rows": [{"ID": "26", "Nickname": "Zoe"}],
            "totalRecords": "27",
            "traversal": {
                "type": "paged",
                "page_index": 2,
                "page_count": 2,
                "has_next_page": False,
                "has_prev_page": True,
            },
        }],
    }
    tables = normalize_table_snapshots(raw)
    assert tables[0]["traversal"]["has_next_page"] is False
    assert tables[0]["traversal"]["has_prev_page"] is True


def test_normalize_preserves_page_size_traversal_fields():
    raw = {
        "url": "http://example.test/admin/reviews",
        "title": "Reviews",
        "tables": [{
            "source": "table",
            "headers": ["ID", "Nickname"],
            "rows": [{"ID": "26", "Nickname": "Zoe"}],
            "totalRecords": "27",
            "traversal": {
                "type": "paged",
                "page_index": 2,
                "page_count": 2,
                "has_next_page": False,
                "has_prev_page": True,
                "page_size": 20,
                "page_size_options": [20, 30, 50, 100],
                "has_page_size_control": True,
                "page_size_menu_open": False,
            },
        }],
    }

    tables = normalize_table_snapshots(raw)

    assert tables[0]["traversal"]["page_size"] == 20
    assert tables[0]["traversal"]["page_size_options"] == [20, 30, 50, 100]
    assert tables[0]["traversal"]["has_page_size_control"] is True
    assert tables[0]["traversal"]["page_size_menu_open"] is False


def test_normalize_preserves_traversal_infinite_scroll():
    raw = {
        "url": "http://example.test/feed",
        "title": "Feed",
        "tables": [{
            "source": "aria-grid",
            "headers": ["User", "Post"],
            "rows": [{"User": "Alice", "Post": "Hello"}],
            "traversal": {
                "type": "scroll",
                "can_scroll_more": True,
                "at_scroll_end": False,
            },
        }],
    }
    tables = normalize_table_snapshots(raw)
    assert tables[0]["traversal"]["type"] == "scroll"
    assert tables[0]["traversal"]["can_scroll_more"] is True
    assert tables[0]["traversal"]["at_scroll_end"] is False


def test_normalize_handles_missing_traversal():
    raw = {
        "url": "http://example.test/simple",
        "title": "Simple",
        "tables": [{
            "source": "table",
            "headers": ["A", "B"],
            "rows": [{"A": "1", "B": "2"}],
        }],
    }
    tables = normalize_table_snapshots(raw)
    assert tables[0]["traversal"] is None


def test_normalize_handles_malformed_traversal():
    raw = {
        "url": "http://example.test/malformed",
        "title": "Bad",
        "tables": [{
            "source": "table",
            "headers": ["A", "B"],
            "rows": [{"A": "1", "B": "2"}],
            "traversal": "not-a-dict",
        }],
    }
    tables = normalize_table_snapshots(raw)
    assert tables[0]["traversal"] is None


# ── viewport: the page-level traversal signal, independent of any table ────────────

def test_normalize_viewport_extracts_page_level_paged_state():
    raw = {
        "url": "http://example.test/admin/reviews",
        "tables": [],
        "viewport": {
            "type": "paged",
            "page_index": 1,
            "page_count": 2,
            "has_next_page": True,
            "has_prev_page": False,
        },
    }
    viewport = normalize_viewport(raw)
    assert viewport == raw["viewport"]


def test_normalize_viewport_extracts_scroll_state_without_any_table():
    """A card/feed page with no <table> at all should still surface a scroll boundary signal —
    this is the whole point of decoupling traversal from tables."""
    raw = {
        "url": "http://example.test/feed",
        "tables": [],
        "viewport": {"type": "scroll", "can_scroll_more": True, "at_scroll_end": False},
    }
    viewport = normalize_viewport(raw)
    assert viewport is not None
    assert viewport["type"] == "scroll"
    assert viewport["can_scroll_more"] is True


def test_normalize_viewport_returns_none_when_unknown():
    raw = {"url": "http://example.test/static", "tables": [], "viewport": {"type": "unknown"}}
    assert normalize_viewport(raw) is None


def test_normalize_viewport_returns_none_when_absent():
    raw = {"url": "http://example.test/static", "tables": []}
    assert normalize_viewport(raw) is None
