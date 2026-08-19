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
    assert "[class*='title' i]" in js
    assert "aria-labelledby" in js
    assert "page-current" in js
    assert "detectPageViewport" in js
    assert "viewport: detectPageViewport" in js
    assert "document.documentElement.scrollHeight" in js
    assert "return { type: 'static' }" in js
    assert "pagerFromControls" in js
    assert "pageNumbers" in js
    assert "const traversal = detectPagerState(parent)" in js
    assert '[aria-current="page"]' in js
    assert "getComputedStyle(item.el)" in js
    assert 'getAttribute("aria-colspan")' in js
    assert 'row.closest("table") === table' in js
    assert 'cell.closest("tr") === row' in js
    assert "\\bpage\\s*(\\d+)" in js
    assert "pagerText.match(/(?:page\\s*)?" not in js


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


def test_normalize_folds_cell_hrefs_into_sibling_url_columns():
    # A linked column carries its href as a "<col>_url" sibling column — no preset "the url"
    # field, and the new column must land in headers so SourceCheck can bind it.
    raw = {
        "url": "http://example.test/admin/orders",
        "title": "Orders",
        "tables": [
            {
                "source": "table",
                "headers": ["ID", "Action"],
                "rows": [
                    ["000000001", "View"],
                    ["000000002", "View"],
                ],
                "rowLinks": [
                    ["", "http://example.test/order/1"],
                    ["", "http://example.test/order/2"],
                ],
                "domRows": 2,
            }
        ],
    }

    tables = normalize_table_snapshots(raw)

    assert tables[0]["headers"] == ["ID", "Action", "Action_url"]
    assert tables[0]["rows"][0] == {
        "ID": "000000001",
        "Action": "View",
        "Action_url": "http://example.test/order/1",
    }
    assert tables[0]["rows"][1]["Action_url"] == "http://example.test/order/2"


def test_normalize_without_links_leaves_headers_unchanged():
    raw = {
        "url": "http://example.test/x",
        "title": "X",
        "tables": [
            {
                "source": "table",
                "headers": ["A", "B"],
                "rows": [["1", "2"]],
                "domRows": 1,
            }
        ],
    }
    tables = normalize_table_snapshots(raw)
    assert tables[0]["headers"] == ["A", "B"]
    assert tables[0]["rows"][0] == {"A": "1", "B": "2"}


def test_normalize_preserves_empty_collection_schema() -> None:
    tables = normalize_table_snapshots({
        "url": "http://example.test/records",
        "title": "Records",
        "tables": [{
            "source": "table",
            "headers": ["Name", "Status"],
            "rows": [],
            "domRows": 1,
            "totalRecords": 0,
            "traversal": {
                "type": "paged",
                "page_index": 1,
                "has_next_page": False,
            },
        }],
    })

    assert tables[0]["headers"] == ["Name", "Status"]
    assert tables[0]["rows"] == []
    assert tables[0]["total_records"] == 0


def test_normalize_discards_total_smaller_than_visible_rows() -> None:
    tables = normalize_table_snapshots({
        "tables": [{
            "headers": ["Name", "Status"],
            "rows": [["A", "open"], ["B", "open"]],
            "totalRecords": 1,
            "traversal": {"type": "paged", "has_next_page": False},
        }],
    })

    assert tables[0]["row_count"] == 2
    assert tables[0]["total_records"] is None


def test_terminal_single_page_reconciles_contradictory_total_to_dom_rows() -> None:
    tables = normalize_table_snapshots({
        "tables": [{
            "headers": ["ID", "Nickname"],
            "rows": [["351", "Emma"], ["349", "Seam Miller"], ["347", "Kai"]],
            "domRows": 3,
            "totalRecords": 4,
            "partial": True,
            "traversal": {
                "type": "paged",
                "page_index": 1,
                "page_count": 1,
                "page_size": 20,
                "has_next_page": False,
            },
        }],
    })

    assert tables[0]["total_records"] == 3
    assert tables[0]["partial"] is False


def test_normalize_url_column_name_avoids_collision():
    # If a literal "Action_url" text column already exists, the link column dedupes.
    raw = {
        "url": "http://example.test/x",
        "title": "X",
        "tables": [
            {
                "source": "table",
                "headers": ["Action", "Action_url"],
                "rows": [["View", "manual"]],
                "rowLinks": [["http://example.test/1", ""]],
                "domRows": 1,
            }
        ],
    }
    tables = normalize_table_snapshots(raw)
    assert tables[0]["headers"] == ["Action", "Action_url", "Action_url_2"]
    assert tables[0]["rows"][0]["Action_url"] == "manual"
    assert tables[0]["rows"][0]["Action_url_2"] == "http://example.test/1"


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
                "page_size_control": {"kind": "selectmenu", "x": 900, "y": 80},
            },
        }],
    }

    tables = normalize_table_snapshots(raw)

    assert tables[0]["traversal"]["page_size"] == 20
    assert tables[0]["traversal"]["page_size_options"] == [20, 30, 50, 100]
    assert tables[0]["traversal"]["page_size_control"]["kind"] == "selectmenu"


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


def test_normalize_preserves_table_viewport_state():
    raw = {
        "url": "http://example.test/product",
        "title": "Product",
        "tables": [{
            "source": "table",
            "headers": ["A", "B"],
            "rows": [{"A": "1", "B": "2"}],
            "in_viewport": False,
            "viewport_pos": "below",
        }],
    }

    table = normalize_table_snapshots(raw)[0]

    assert table["in_viewport"] is False
    assert table["viewport_pos"] == "below"


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
