from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from gui_agent.core.tool_agent.contracts import WorkerOutcome, WorkerSpec, WorkerStrategy
from gui_agent.core.tool_agent.strategy import Reflector


class _JsonModel:
    def __init__(self, *values: dict) -> None:
        self.values = list(values)
        self.calls = 0
        self.bind_kwargs: dict = {}

    def bind(self, **kwargs):
        self.bind_kwargs = kwargs
        return self

    def invoke(self, _messages):
        value = self.values[min(self.calls, len(self.values) - 1)]
        self.calls += 1
        return SimpleNamespace(content=json.dumps(value))


def _original_spec() -> WorkerSpec:
    return WorkerSpec.model_validate({
        "profile": "operator",
        "goal": "Display the requested forecast",
        "success_criteria": ["The requested forecast is visible"],
        "strategy": {
            "approach": "Search for the exact requested forecast.",
        },
    })


def _candidate(approach: str = "Use an alternative visible path.") -> dict:
    return {"approach": approach}


def _replace(candidate: dict, reason: str = "Executable alternative") -> dict:
    return {"decision": "replace", "reason": reason, "strategy": candidate}


def _decide(model: _JsonModel, **context):
    events = []
    original = _original_spec()
    result = Reflector(model).reflect(
        context={
            "attempted_strategies": [],
            **context,
        },
        original_strategy=original.strategy,
        on_event=lambda event, **payload: events.append((event, payload)),
    )
    return original, result.strategy, result.reason, events


def test_strategy_returns_only_a_replacement_strategy() -> None:
    model = _JsonModel(_replace(_candidate()))

    original, selected, reason, events = _decide(model)

    assert selected is not None
    assert reason == "Executable alternative"
    assert isinstance(selected, WorkerStrategy)
    assert original.goal == "Display the requested forecast"
    assert original.success_criteria == ["The requested forecast is visible"]
    assert selected.approach == "Use an alternative visible path."
    assert model.calls == 1
    assert model.bind_kwargs["max_tokens"] == 600
    assert events[0][0] == "reflection_decision"
    assert events[0][1]["decision"] == "revise_approach"


def test_strategy_repairs_one_invalid_candidate() -> None:
    model = _JsonModel(
        _replace({"actions": []}),
        _replace(_candidate(), "Repaired executable candidate"),
    )

    _original, selected, reason, _events = _decide(model)

    assert selected is not None
    assert reason == "Repaired executable candidate"
    assert model.calls == 2


@pytest.mark.parametrize("invalid", [
    "Navigate to the source, select Shenzhen, then inspect it.",
    "Use one source, then inspect its result list.",
    "open_url https://weather.example/forecast/location-id",
])
def test_strategy_repairs_procedural_approaches(invalid: str) -> None:
    model = _JsonModel(
        _replace(_candidate(invalid)),
        _replace(_candidate("Different public forecast source")),
    )

    _original, selected, _reason, _events = _decide(model)

    assert selected is not None
    assert selected.approach == "Different public forecast source"
    assert model.calls == 2


def test_strategy_stops_after_repeated_approach_repair() -> None:
    model = _JsonModel(
        _replace(_candidate("Search for the exact requested forecast.")),
        _replace(_candidate("Search for the exact requested forecast.")),
    )

    _original, selected, reason, events = _decide(model)

    assert selected is None
    assert "valid recommendation" in reason
    assert model.calls == 2
    assert events[0][1]["decision"] == "stop"


def test_strategy_replacement_contains_only_an_approach() -> None:
    candidate = {
        "approach": "Use a different public discovery surface.",
        "actions": [],
    }

    _original, selected, _reason, events = _decide(
        _JsonModel(_replace(candidate), _replace(candidate)),
    )

    assert selected is None
    assert any("Extra inputs" in item for item in events[0][1]["diagnostics"])


def test_strategy_can_stop_without_a_candidate() -> None:
    model = _JsonModel({
        "decision": "stop",
        "reason": "Every evidenced path has been disproved.",
        "strategy": None,
    })

    _original, selected, reason, events = _decide(model)

    assert selected is None
    assert "disproved" in reason
    assert model.calls == 1
    assert events[0][1]["decision"] == "stop"


def test_reflector_returns_typed_reconcile_without_an_approach() -> None:
    model = _JsonModel({
        "diagnosis": {
            "kind": "state_conflict",
            "evidence_refs": ["step:2"],
            "reason": "The receipt and current state need reduction.",
        },
        "recommendation": {
            "decision": "reconcile_state",
            "approach": None,
            "preserve_progress": True,
            "invalidate": [],
        },
    })
    events = []

    result = Reflector(model).reflect(
        context={"attempted_approaches": []},
        original_strategy=_original_spec().strategy,
        on_event=lambda event, **payload: events.append((event, payload)),
    )

    assert result.decision == "reconcile_state"
    assert result.strategy is None
    assert result.preserve_progress is True
    assert events[0][0] == "reflection_decision"
