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
            tables=tables,
            applied_filters=filters,
        ),
        lookup_request,
    )

    assert is_lookup_scope(scope) is (fingerprint is not None)
    if scope is not None:
        assert scope["surface_fingerprint"] == fingerprint
