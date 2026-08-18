from gui_agent.core.tool_agent.filter_state import (
    AppliedFilterState,
    compile_filter_predicates,
    display_filter_predicates,
    match_filter_state,
    strip_contains_suffix,
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


def test_contains_suffix_lowers_to_semantic_predicate_on_base_field() -> None:
    predicates = compile_filter_predicates(
        {"review_text_contains": "ear cups being small"}
    )
    assert strip_contains_suffix("review_text_contains") == "review_text"
    assert strip_contains_suffix("review_text") == "review_text"
    assert list(predicates) == ["review text"]
    assert predicates["review text"].operator == "contains"
    assert predicates["review text"].values == ["ear cups being small"]


def test_numeric_bound_max_compiles_to_lte() -> None:
    # "rating of 3 or less" must be a range predicate, never exact equality
    # (a bare `star_rating: 3` would drop the 1-star reviewers — live task 25).
    predicates = compile_filter_predicates({"star_rating": {"max": 3}})
    assert predicates["star rating"].operator == "lte"
    assert predicates["star rating"].values == ["3"]


def test_display_presents_contains_on_base_field() -> None:
    # Rendered as the bare base-field phrase so perception matches by meaning,
    # never as a literal "must contain" that triggers substring matching.
    assert display_filter_predicates(
        {"review_text_contains": "ear cups being small", "date": "2026-08-15"}
    ) == {
        "review_text": "ear cups being small",
        "date": "2026-08-15",
    }
