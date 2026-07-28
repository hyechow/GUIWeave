from gui_agent.core.acceptance import AcceptanceMatcher


def test_exact_matcher_returns_met_for_equal_typed_structures() -> None:
    expected = {"quantity": {"operator": "eq", "values": ["3"]}}

    result = AcceptanceMatcher.exact(
        expected,
        {"quantity": {"operator": "eq", "values": ["3"]}},
        evidence_complete=True,
    )

    assert result.status == "met"
    assert result.expected == result.actual


def test_exact_matcher_returns_unmet_for_different_complete_structures() -> None:
    result = AcceptanceMatcher.exact(
        {"status": "complete"},
        {"status": "pending"},
        evidence_complete=True,
    )

    assert result.status == "unmet"
    assert result.reason == "canonical structures differ"


def test_exact_matcher_returns_unknown_without_complete_evidence() -> None:
    result = AcceptanceMatcher.exact(
        {"status": "complete"},
        {"status": "pending"},
        evidence_complete=False,
    )

    assert result.status == "unknown"

