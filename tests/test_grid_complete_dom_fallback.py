"""Regression: browser foreach row-collection (read_grid_complete) must fall back to the
DOM <table> snapshot when the AX semantic tree exposes no matching grid.

Root case = WebArena shopping_admin task 204: after drilling to an order detail page, the
"Items Ordered" table is a plain DOM data-table that read_tables() captures but the AX tree
(read_grid_from_tree) does not surface as a grid. Without the fallback the second foreach
collected 0 rows ("无可迭代行") and the whole most-recent-order read failed.
"""

from __future__ import annotations

from gui_agent.adapters.browser.page_read import read_grid_complete
from gui_agent.core.schemas import Observation

# Shape mirrors normalize_table_snapshots() output for a Magento order detail page: the
# Items Ordered table plus unrelated detail tables; rowspan splits produce empty-Price phantoms.
_ITEMS_TABLE = {
    "caption": "Items Ordered",
    "headers": ["Product", "Item Status", "Price", "Qty", "Subtotal"],
    "rows": [
        {"Product": "Proteus Fitness Jackshirt SKU: MJ12-M-Blue", "Price": "$45.00",
         "Item Status": "Ordered", "Qty": "1", "Subtotal": "$45.00"},
        {"Product": "Ordered", "Price": "", "Item Status": "", "Qty": "", "Subtotal": ""},
        {"Product": "Ida Workout Parachute Pant SKU: WP03-28-Blue", "Price": "$38.40",
         "Item Status": "Ordered", "Qty": "1", "Subtotal": "$38.40"},
    ],
}
_ACCOUNT_TABLE = {
    "caption": "Account Information",
    "headers": ["Customer Name", "Email"],
    "rows": [{"Customer Name": "Ava Brown", "Email": "ava@example.com"}],
}


def test_read_grid_complete_falls_back_to_dom_table_when_ax_tree_empty():
    obs = Observation(
        png_bytes=b"",
        source="eval",
        semantic_tree=None,            # AX tree exposes no grid (the order-detail case)
        tables=[_ACCOUNT_TABLE, _ITEMS_TABLE],
    )
    rows = read_grid_complete(obs, ["Product", "Price"])
    assert rows is not None, "should not give up when a DOM table covers the returns"
    # _best_table must pick Items Ordered (matches Product+Price), not Account Information.
    products = [r.get("Product", "") for r in rows]
    assert any("Proteus" in p for p in products)
    assert any("Ida" in p for p in products)
    # Phantom empty-Price rows are preserved here; the downstream data_query filters Price != ''.
    assert any(r.get("Price") in ("", None) for r in rows)


def test_read_grid_complete_returns_none_when_no_table_and_no_tree():
    obs = Observation(png_bytes=b"", source="eval", semantic_tree=None, tables=None)
    assert read_grid_complete(obs, ["Product", "Price"]) is None


def test_read_grid_complete_dom_fallback_requires_column_match():
    # A DOM table that doesn't cover the requested columns → None (caller goes interactive).
    obs = Observation(png_bytes=b"", source="eval", semantic_tree=None, tables=[_ACCOUNT_TABLE])
    assert read_grid_complete(obs, ["Product", "Price"]) is None


class _PagerClient:
    def __init__(self):
        self.moves = 0
        self.opened = 0
        self.selected: list[str] = []

    def _cdp_send(self, method, params):
        assert method == "Runtime.evaluate"
        self.moves += 1
        return {"result": {"value": True}}

    def wait_settled(self, action_type=None):
        return (0.0, False)

    def tap(self, x, y):
        self.opened += 1
        return "OK tap"

    def select_option(self, x, y, text):
        self.selected.append(text)
        return f"OK select_option {text}"


class _Perception:
    def __init__(self, observation):
        self.observation = observation

    def observe(self):
        return self.observation


class _Bundle:
    def __init__(self, observations):
        self.observations = list(observations)

    def make_perception(self, platform, path):
        return _Perception(self.observations.pop(0))


class _Platform:
    def __init__(self, client):
        self.client = client


def _paged_products(page, rows, *, has_next, total=3, traversal_extra=None):
    traversal = {
        "type": "paged",
        "page_index": page,
        "has_next_page": has_next,
        "has_prev_page": page > 1,
    }
    traversal.update(traversal_extra or {})
    return {
        "path": "#product-grid",
        "headers": ["Name", "Type", "Action_url"],
        "rows": rows,
        "total_records": total,
        "traversal": traversal,
    }


def _page_size_obs(rows, *, size, has_next):
    return Observation(
        png_bytes=b"",
        source="eval",
        tables=[_paged_products(
            1, rows, has_next=has_next,
            traversal_extra={
                "page_size": size,
                "page_size_options": [2, 3, 10],
                "page_size_control": {"kind": "selectmenu", "x": 800, "y": 400},
            },
        )],
    )


def test_complete_grid_binds_to_table_pager_not_page_viewport(tmp_path):
    """A page-level scroll signal must not hide or drive a paginated grid."""
    first = Observation(
        png_bytes=b"",
        source="eval",
        semantic_tree=None,
        tables=[
            _paged_products(
                1,
                [
                    {"Name": "Item A", "Type": "Simple", "Action_url": "/a"},
                    {"Name": "Item B", "Type": "Simple", "Action_url": "/b"},
                ],
                has_next=True,
            )
        ],
        viewport={"type": "scroll", "can_scroll_more": False, "at_scroll_end": True},
    )
    second = Observation(
        png_bytes=b"",
        source="eval",
        semantic_tree=None,
        tables=[
            _paged_products(
                2,
                [{"Name": "Target", "Type": "Owner", "Action_url": "/target"}],
                has_next=False,
            )
        ],
        viewport={"type": "scroll", "can_scroll_more": False, "at_scroll_end": True},
    )
    client = _PagerClient()

    rows = read_grid_complete(
        first,
        ["name", "type", "detail_url"],
        bundle=_Bundle([second]),
        platform=_Platform(client),
        log_dir=tmp_path,
    )

    assert rows == [
        {"name": "Item A", "type": "Simple", "detail_url": "/a"},
        {"name": "Item B", "type": "Simple", "detail_url": "/b"},
        {"name": "Target", "type": "Owner", "detail_url": "/target"},
    ]
    assert client.moves == 1


def test_complete_grid_uses_covering_page_size_before_paginating(tmp_path):
    first = _page_size_obs([
        {"Name": "Item A", "Type": "Simple", "Action_url": "/a"},
        {"Name": "Item B", "Type": "Simple", "Action_url": "/b"},
    ], size=2, has_next=True)
    expanded = _page_size_obs([
        {"Name": "Item A", "Type": "Simple", "Action_url": "/a"},
        {"Name": "Item B", "Type": "Simple", "Action_url": "/b"},
        {"Name": "Owner", "Type": "Configurable", "Action_url": "/owner"},
    ], size=3, has_next=False)
    client = _PagerClient()

    rows = read_grid_complete(
        first,
        ["name", "type", "detail_url"],
        bundle=_Bundle([expanded]),
        platform=_Platform(client),
        log_dir=tmp_path,
    )

    assert rows is not None and rows[-1]["detail_url"] == "/owner"
    assert client.opened == 1
    assert client.selected == ["3"]
    assert client.moves == 0
