from __future__ import annotations

import pytest

from gui_agent.core.filter_contract import (
    AppliedFilterState,
    compare_filter_state,
    compile_filter_predicates,
)
from gui_agent.core.run.lookup_scope import (
    is_lookup_scope,
    resolve_lookup_scope,
)
from gui_agent.core.schemas import CollectionIntent, Observation


def _table(path, caption, headers):
    return {
        "path": path,
        "caption": caption,
        "headers": headers,
        "rows": [{field: field for field in headers}],
    }


def _filter_state(filters, *, coverage="complete"):
    return AppliedFilterState(
        predicates=compile_filter_predicates(filters or {}),
        coverage=coverage,
        source="test",
    )


def _filters_match(observation, filters) -> bool:
    return compare_filter_state(
        compile_filter_predicates(filters),
        observation.applied_filter_state,
    ) is True


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
        CollectionIntent(phase="locate", **lookup_request),
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
        CollectionIntent(phase="locate", entity="Orders List"),
    )

    assert scope is not None
    assert scope["surface_fingerprint"] == "table:#orders"


def test_lookup_resolves_unique_collection_from_required_field_schema() -> None:
    scope = resolve_lookup_scope(
        Observation(
            png_bytes=b"png",
            source="browser",
            title="History",
            tables=[_table("#audit", "", ["Actor", "Outcome"])],
        ),
        CollectionIntent(
            phase="locate",
            entity="Audit Records",
            required_fields=["Actor", "Outcome"],
        ),
    )

    assert scope is not None
    assert scope["surface_fingerprint"] == "table:#audit"


def test_lookup_addresses_one_raw_cell_collection_without_inventing_schema() -> None:
    scope = resolve_lookup_scope(
        Observation(
            png_bytes=b"png",
            source="android",
            collection_regions=[{
                "ref": "android-collection:0",
                "surface_fingerprint": "android-collection:feed",
                "cells": [],
                "bounds": (0, 0, 1000, 1000),
                "traversal": {"type": "scroll"},
            }],
        ),
        CollectionIntent(
            phase="locate",
            entity="Posts",
            required_fields=["author", "content"],
        ),
    )

    assert scope == {
        "kind": "resolved_collection",
        "entity": "Posts",
        "surface_fingerprint": "android-collection:feed",
        "available_fields": [],
        "projection": "cells",
    }


def test_lookup_does_not_resolve_single_collection_from_generic_word_only() -> None:
    scope = resolve_lookup_scope(
        Observation(
            png_bytes=b"png",
            source="browser",
            title="Orders / Operations / Sales / Magento Admin",
            tables=[_table("#orders", "", ["Status", "Purchase Date"])],
        ),
        CollectionIntent(phase="locate", entity="Products List"),
    )

    assert scope is None


def test_lookup_is_pure_locate_and_does_not_gate_on_filters() -> None:
    """Lookup locates the collection by identity regardless of applied filters.

    Narrowing a view is the ``constrain`` statement's job — see
    ``test_applied_filters_match_is_the_constrain_predicate``. Lookup must not
    deadlock waiting for a filter it is forbidden to apply.
    """
    observation = Observation(
        png_bytes=b"png",
        source="browser",
        title="Orders / Operations / Sales / Magento Admin",
        tables=[_table("#orders", "", ["Status", "Purchase Date"])],
        applied_filters={"Status": "Pending"},
    )

    scope = resolve_lookup_scope(
        observation,
        CollectionIntent(phase="locate", entity="Orders"),
    )

    assert scope is not None
    assert scope["surface_fingerprint"] == "table:#orders"
    assert "filters" not in scope


def test_applied_filters_match_is_the_constrain_predicate() -> None:
    def observed(applied):
        return Observation(
            png_bytes=b"png",
            source="browser",
            tables=[_table("#orders", "Orders", ["Status"])],
            applied_filters=applied,
            applied_filter_state=(
                _filter_state(applied)
                if applied is not None
                else None
            ),
        )

    # Missing adapter evidence is unknown, including for an empty request.
    assert _filters_match(observed(None), {}) is False
    assert _filters_match(observed({}), {}) is True
    # Requested filter active (semantic + case insensitive) → satisfied.
    assert _filters_match(observed({"Status": "Complete"}), {"Status": "complete"}) is True
    # Requested filter absent or mismatched → not satisfied.
    assert _filters_match(observed({"Status": "Pending"}), {"Status": "Complete"}) is False
    assert _filters_match(observed(None), {"Status": "Complete"}) is False


def test_compile_filter_predicates_folds_nested_range_mapping() -> None:
    nested = compile_filter_predicates({
        "Status": "Complete",
        "Purchase Date": {
            "from": "01/01/2023",
            "to": "05/31/2023",
        },
    })
    split = compile_filter_predicates({
        "Status": "Complete",
        "Purchase Date from": "01/01/2023",
        "Purchase Date to": "05/31/2023",
    })

    assert nested == split
    date_predicate = nested["purchase date"]
    assert date_predicate.operator == "range"
    assert date_predicate.values == ["2023-01-01", "2023-05-31"]


def test_date_filter_values_compare_by_typed_value_not_display_format() -> None:
    iso = compile_filter_predicates({
        "Purchase Date": {"from": "2023-01-01", "to": "2023-05-31"},
    })
    display = compile_filter_predicates({
        "Purchase Date": {"from": "01/01/2023", "to": "05/31/2023"},
    })

    assert iso == display


def test_numeric_filter_contract_uses_a_canonical_scalar() -> None:
    integer = compile_filter_predicates({"Quantity": 3})
    decimal = compile_filter_predicates({"Quantity": 3.0})

    assert integer == decimal
    assert integer["quantity"].operator == "eq"
    assert integer["quantity"].values == ["3"]


def test_numeric_bounds_use_the_same_canonical_value_format() -> None:
    predicate = compile_filter_predicates({
        "Quantity": {"from": "3.0000", "to": 3},
    })["quantity"]

    assert predicate.operator == "eq"
    assert predicate.values == ["3"]


def test_contract_text_is_not_reinterpreted_as_a_numeric_display_range() -> None:
    predicate = compile_filter_predicates({"Name": "3 - 3"})["name"]

    assert predicate.operator == "eq"
    assert predicate.values == ["3 - 3"]


def test_applied_filters_require_exact_predicate_set() -> None:
    observation = Observation(
        png_bytes=b"png",
        source="browser",
        applied_filter_state=_filter_state({
            "Status": "Complete",
            "Store": "Main",
        }),
    )
    assert _filters_match(observation, {"Status": "Complete"}) is False


def test_range_suffixes_do_not_collide_with_ordinary_field_names() -> None:
    predicates = compile_filter_predicates({
        "Admin": "alice",
        "Photo": "present",
    })
    assert {
        field: predicate.operator
        for field, predicate in predicates.items()
    } == {"admin": "eq", "photo": "eq"}


def test_lookup_waits_until_required_fields_are_exposed() -> None:
    request = CollectionIntent(
        phase="locate",
        entity="Orders",
        required_fields=["Customer Email", "Status"],
    )
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
