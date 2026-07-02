# -*- coding: utf-8 -*-
"""URL-ish grid columns match by CAPABILITY (cells carry link hrefs), not by header text.

Live 778 run 20260702_232803: the decomposer declared `detail_url` but the Magento grid header is
"Action" — name-only fuzzy matching dropped the column, every collected row lacked the key, and the
foreach died on the column-completeness net before ever reaching the checkpoint. The decomposer
names the row-link column unpredictably (detail_url/action_url/edit_url/url/link); the LINK column
itself is directly observable via cell hrefs."""
from __future__ import annotations

from gui_agent.adapters.browser.semantic_page import read_grid_from_tree


def _node(role, key="", depth=0, ref=0, value="", url=""):
    return {"role": role, "key": key, "value": value, "url": url, "ref": ref, "depth": depth}


def _magento_products_tree():
    """Products grid shape: header Action column renders an Edit link per row."""
    headers = ["ID", "Name", "SKU", "Price", "Action"]
    tree = [_node("table", depth=0), _node("row", depth=1)]
    tree += [_node("columnheader", key=h, depth=2) for h in headers]
    rows = [
        ("1842", "Sahara Leggings-28-Gray", "WP05-28-Gray", "$75.00", "http://x/edit/id/1842/"),
        ("1846", "Sahara Leggings-29-Red", "WP05-29-Red", "$75.00", "http://x/edit/id/1846/"),
    ]
    for rid, name, sku, price, href in rows:
        tree.append(_node("row", depth=1))
        for cell in (rid, name, sku, price):
            tree.append(_node("cell", key=cell, depth=2))
        tree.append(_node("cell", key="Edit", depth=2))
        tree.append(_node("link", key="Edit", depth=3, url=href))   # link child carries the href
    return tree


def test_detail_url_matches_link_column_by_capability():
    rows = read_grid_from_tree(_magento_products_tree(), ["sku", "detail_url"])
    assert rows is not None and len(rows) == 2
    assert rows[0]["sku"] == "WP05-28-Gray"
    assert rows[0]["detail_url"] == "http://x/edit/id/1842/"       # header said "Action"
    assert rows[1]["detail_url"] == "http://x/edit/id/1846/"


def test_name_matched_url_field_still_wins_over_capability():
    # action_url fuzzy-matches header "Action" (ratio ≥0.75) — the name match must keep priority.
    rows = read_grid_from_tree(_magento_products_tree(), ["action_url"])
    assert rows is not None and rows[0]["action_url"] == "http://x/edit/id/1842/"


def test_non_url_fields_unaffected_and_no_links_no_match():
    # a grid with no link cells: URL-ish field stays unmatched (no bogus mapping)…
    headers = ["ID", "Name"]
    tree = [_node("table", depth=0), _node("row", depth=1)]
    tree += [_node("columnheader", key=h, depth=2) for h in headers]
    tree.append(_node("row", depth=1))
    tree += [_node("cell", key="1", depth=2), _node("cell", key="Widget", depth=2)]
    rows = read_grid_from_tree(tree, ["name", "detail_url"])
    assert rows is not None
    assert rows[0]["name"] == "Widget"
    assert rows[0].get("detail_url", "") == ""                     # absent/empty, not hallucinated
