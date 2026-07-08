from __future__ import annotations

from types import SimpleNamespace

import scripts.validator_retry_efficacy as efficacy


def test_decompose_trace_preserves_attempts_when_decompose_raises(monkeypatch):
    def fake_decompose(*_args, attempt_observer=None, **_kwargs):
        assert attempt_observer is not None
        attempt_observer(0, [SimpleNamespace(code="FIRST_CODE")])
        attempt_observer(1, [SimpleNamespace(code="FINAL_CODE")])
        raise RuntimeError("compile failed")

    monkeypatch.setattr(efficacy, "_case_knowledge", lambda _case: None)
    monkeypatch.setattr(efficacy, "decompose", fake_decompose)

    trace = efficacy._decompose_with_trace({"goal": "x"})

    assert trace.attempts == [["FIRST_CODE"], ["FINAL_CODE"]]
    assert trace.error == "RuntimeError: compile failed"

    stats = efficacy.aggregate([trace.attempts])
    assert stats["FIRST_CODE"].fed_back == 1
    assert stats["FIRST_CODE"].cleared == 1
    assert stats["FINAL_CODE"].shipped == 1
