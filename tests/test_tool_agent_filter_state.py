from gui_agent.core.tool_agent.filter_state import (
    AppliedFilterState,
    compile_filter_predicates,
    match_filter_state,
)


def test_unspaced_date_range_matches_padded_display() -> None:
    requested = compile_filter_predicates({"Purchase Date": "01/01/2022-11/30/2022"})
    applied = compile_filter_predicates({"Purchase Date": "01/1/2022 - 11/30/2022"})
    assert requested == applied
    assert requested["purchase date"].operator == "range"
    assert requested["purchase date"].values == ["2022-01-01", "2022-11-30"]
    assert (
        match_filter_state(
            requested,
            AppliedFilterState(predicates=applied, coverage="complete", source="chips"),
        )
        == "met"
    )
