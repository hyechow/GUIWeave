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
