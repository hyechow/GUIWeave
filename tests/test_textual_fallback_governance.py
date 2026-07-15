from __future__ import annotations

import pytest

from gui_agent.core.orchestrator._validator.governance import (
    TEXTUAL_FALLBACK_HEURISTIC_SAMPLES,
    TEXTUAL_FALLBACK_VALIDATOR_CODES,
)


def _samples(kind: str) -> list[dict[str, object]]:
    return [sample for sample in TEXTUAL_FALLBACK_HEURISTIC_SAMPLES if sample.get("kind") == kind]


def test_textual_fallback_heuristic_registry_has_metadata():
    ids = [str(sample.get("id") or "") for sample in TEXTUAL_FALLBACK_HEURISTIC_SAMPLES]
    assert len(ids) == len(set(ids))
    assert ids
    for sample in TEXTUAL_FALLBACK_HEURISTIC_SAMPLES:
        assert sample.get("id")
        assert sample.get("kind")
        assert sample.get("owner")
        assert sample.get("retire_when")
        assert sample.get("trigger") or sample.get("statements") or sample.get("statement_name")
        code = sample.get("validator_code")
        if code is not None:
            assert code in TEXTUAL_FALLBACK_VALIDATOR_CODES


@pytest.mark.parametrize("sample", _samples("retrieval_field_extract"), ids=lambda s: str(s["id"]))
def test_retrieval_field_stopword_extract_samples(sample):
    from gui_agent.core.orchestrator._validator.retrieval import _extract_retrieval_fields

    assert _extract_retrieval_fields(str(sample["trigger"])) == sample["expected"]


@pytest.mark.parametrize("sample", _samples("retrieval_same_target"), ids=lambda s: str(s["id"]))
def test_retrieval_same_target_samples(sample):
    from gui_agent.core.orchestrator._validator.retrieval import _mentions_same_retrieval_target

    assert _mentions_same_retrieval_target(str(sample["trigger"])) is sample["expected"]


@pytest.mark.parametrize("sample", _samples("retrieval_field_normalize"), ids=lambda s: str(s["id"]))
def test_retrieval_field_stopword_normalize_samples(sample):
    from gui_agent.core.orchestrator._validator.retrieval import _normalize_retrieval_field

    assert _normalize_retrieval_field(str(sample["trigger"])) == sample["expected"]


def test_all_textual_fallback_validator_codes_have_heuristic_samples():
    sampled_codes = {
        str(sample["validator_code"])
        for sample in TEXTUAL_FALLBACK_HEURISTIC_SAMPLES
        if sample.get("validator_code")
    }
    assert TEXTUAL_FALLBACK_VALIDATOR_CODES <= sampled_codes
