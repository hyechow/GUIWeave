import pytest

from gui_agent.adapters.browser.filter_state import typed_applied_filter_state


@pytest.mark.parametrize(
    ("filters", "meta", "coverage"),
    [
        (None, {"source": "none", "indicator_channel": "present"}, "complete"),
        (None, {"source": "none", "indicator_channel": "absent"}, "unavailable"),
        ({"Status": "Complete"}, {"source": "adapter_state"}, "complete"),
    ],
)
def test_filter_channels_determine_typed_coverage(filters, meta, coverage) -> None:
    state = typed_applied_filter_state(filters, meta)
    assert state.coverage == coverage
    if filters:
        assert state.predicates["status"].values == ["complete"]
    else:
        assert state.predicates == {}


def test_equal_bound_numeric_chip_projects_to_exact_scalar_predicate() -> None:
    state = typed_applied_filter_state(
        {"Quantity": "3.0000 - 3"},
        {"source": "chips"},
    )

    assert state.predicates["quantity"].operator == "eq"
    assert state.predicates["quantity"].values == ["3"]
