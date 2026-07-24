from __future__ import annotations

import pytest

from gui_agent.core.run.lookup_scope import is_lookup_scope, resolve_lookup_scope
from gui_agent.core.schemas import Observation


def _table(path, caption, headers):
    return {
        "path": path,
        "caption": caption,
        "headers": headers,
        "rows": [{field: field for field in headers}],
    }


@pytest.mark.parametrize(
    ("lookup_request", "tables", "filters", "fingerprint"),
    [
        (
            {"entity": "Top Search Terms", "field": "name"},
            [
                _table("#recent", "Recent Orders", ["Order", "Total"]),
                _table("#terms", "Top Search Terms", ["Search Term", "Uses"]),
            ],
            None,
            "table:#terms",
        ),
        (
            {"entity": "size 28 Sahara leggings", "field": "Name", "fallback": "Sahara"},
            [_table("#products", "Products", ["Name", "SKU"])],
            {"Name": "Sahara"},
            "table:#products",
        ),
        (
            {"entity": "Orders List", "field": "name"},
            [_table("#orders", "", ["Status", "Purchase Date"])],
            None,
            "table:#orders",
        ),
        (
            {"entity": "Missing", "field": "Name"},
            [
                _table("#one", "One", ["Name"]),
                _table("#two", "Two", ["Name"]),
            ],
            None,
            None,
        ),
    ],
)
def test_lookup_resolves_only_an_exact_or_proven_filtered_collection(
    lookup_request, tables, filters, fingerprint,
) -> None:
    scope = resolve_lookup_scope(
        Observation(
            png_bytes=b"png",
            source="browser",
            title="Orders" if lookup_request["entity"] == "Orders List" else "",
            tables=tables,
            applied_filters=filters,
        ),
        lookup_request,
    )

    assert is_lookup_scope(scope) is (fingerprint is not None)
    if scope is not None:
        assert scope["surface_fingerprint"] == fingerprint


def test_lookup_resolves_single_collection_from_business_word_in_page_title() -> None:
    scope = resolve_lookup_scope(
        Observation(
            png_bytes=b"png",
            source="browser",
            title="Orders / Operations / Sales / Magento Admin",
            tables=[_table("#orders", "", ["Status", "Purchase Date"])],
        ),
        {"entity": "Orders List", "field": "name"},
    )

    assert scope is not None
    assert scope["surface_fingerprint"] == "table:#orders"


def test_lookup_does_not_resolve_single_collection_from_generic_word_only() -> None:
    scope = resolve_lookup_scope(
        Observation(
            png_bytes=b"png",
            source="browser",
            title="Orders / Operations / Sales / Magento Admin",
            tables=[_table("#orders", "", ["Status", "Purchase Date"])],
        ),
        {"entity": "Products List", "field": "name"},
    )

    assert scope is None


def test_lookup_waits_until_requested_filters_are_applied() -> None:
    observation = Observation(
        png_bytes=b"png",
        source="browser",
        title="Orders / Operations / Sales / Magento Admin",
        tables=[_table("#orders", "", ["Status", "Purchase Date"])],
        applied_filters={"Status": "Pending"},
    )

    scope = resolve_lookup_scope(
        observation,
        {
            "entity": "Orders",
            "field": "name",
            "filters": {"Status": "Complete"},
        },
    )

    assert scope is None


def test_lookup_scope_records_verified_filters() -> None:
    scope = resolve_lookup_scope(
        Observation(
            png_bytes=b"png",
            source="browser",
            title="Orders / Operations / Sales / Magento Admin",
            tables=[_table("#orders", "", ["Status", "Purchase Date"])],
            applied_filters={"Status": "Complete"},
        ),
        {
            "entity": "Orders",
            "field": "name",
            "filters": {"Status": "Complete"},
        },
    )

    assert scope is not None
    assert scope["filters"] == {"Status": "Complete"}


def test_lookup_waits_until_required_fields_are_exposed() -> None:
    request = {
        "entity": "Orders",
        "field": "name",
        "filters": {"Status": "Complete"},
        "required_fields": ["Customer Email", "Status"],
    }
    missing = resolve_lookup_scope(
        Observation(
            png_bytes=b"png",
            source="browser",
            title="Orders / Operations / Sales / Magento Admin",
            tables=[_table("#orders", "", ["ID", "Status"])],
            applied_filters={"Status": "Complete"},
        ),
        request,
    )
    ready = resolve_lookup_scope(
        Observation(
            png_bytes=b"png",
            source="browser",
            title="Orders / Operations / Sales / Magento Admin",
            tables=[_table("#orders", "", ["ID", "Customer Email", "Status"])],
            applied_filters={"Status": "Complete"},
        ),
        request,
    )

    assert missing is None
    assert ready is not None
    assert "Customer Email" in ready["available_fields"]
