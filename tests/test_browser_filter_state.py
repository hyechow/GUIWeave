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
