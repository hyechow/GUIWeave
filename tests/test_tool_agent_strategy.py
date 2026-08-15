from __future__ import annotations

import json
from types import SimpleNamespace

from gui_agent.core.tool_agent.contracts import WorkerSpec
from gui_agent.core.tool_agent.runtime import ToolAgentRuntime
from gui_agent.core.tool_agent.strategy import StrategyPlanner


class _JsonModel:
    def __init__(self, value: dict) -> None:
        self.value = value
        self.calls = 0

    def bind(self, **_kwargs):
        return self

    def invoke(self, _messages):
        self.calls += 1
        return SimpleNamespace(content=json.dumps(self.value))


def _candidate(*, capability: str = "type", estimated_steps: int = 2) -> dict:
    return {
        "hypothesis": "A distinct source can expose the requested result.",
        "invalidated_assumption": "The current source is the only path.",
        "strategy": "Use a distinct public source and verify its leading result.",
        "actions": [{
            "name": "enter_alternative_query",
            "capability": capability,
            "fixed_args": {"text": "深圳天气"},
            "input_args": {
                "x": "search_box_x",
                "y": "search_box_y",
                "description": "Visible alternative search field",
            },
        }],
        "expected_progress": "A relevant result appears in the leading region.",
        "disconfirming_evidence": "The leading results remain off target.",
        "evidence_basis": ["The prior source was blocked."],
        "estimated_steps": estimated_steps,
    }


def _original_spec() -> WorkerSpec:
    return WorkerSpec.model_validate({
        "profile": "operator",
        "goal": "Display the requested forecast",
        "strategy": "Use the current source",
        "success_criteria": ["The requested forecast is visible"],
        "data_requirements": [],
        "actions": [{
            "name": "enter_query",
            "capability": "type",
            "description": "Enter the query in the current search field.",
            "fixed_args": {"text": "深圳天气"},
        }],
    })


def test_strategy_planner_isolates_and_normalizes_candidate_drafts() -> None:
    proposer = _JsonModel({
        "candidates": [_candidate(), _candidate(capability="teleport")],
    })
    selector = _JsonModel({
        "decision": "attempt", "chosen_index": 0, "reason": "Executable alternative",
    })
    events = []
    planner = StrategyPlanner(proposer, selector=selector)

    selected, _reason = planner.choose(
        context={"remaining_step_budget": 4},
        original_spec=_original_spec(),
        preserve_acquisition_filters=True,
        validate=lambda spec: ToolAgentRuntime._worker_revision_issues(
            _original_spec(), spec,
        ),
        on_event=lambda event, **payload: events.append((event, payload)),
    )

    assert selected is not None
    assert selected.actions[0].description == "Visible alternative search field"
    assert selected.actions[0].input_args == {}
    assert selected.actions[0].exposed_args == ["x", "y", "description"]
    reviewed = events[0][1]["candidates"]
    assert [item["proposal_index"] for item in reviewed] == [0, 1]
    assert reviewed[0]["diagnostics"] == []
    assert "teleport" in reviewed[1]["diagnostics"][0]
    assert events[1][1]["chosen_proposal_index"] == 0
    assert proposer.calls == selector.calls == 1


def test_strategy_planner_skips_selector_when_no_candidate_fits_budget() -> None:
    proposer = _JsonModel({"candidates": [_candidate(estimated_steps=2)]})
    selector = _JsonModel({
        "decision": "attempt", "chosen_index": 0, "reason": "Should not run",
    })
    planner = StrategyPlanner(proposer, selector=selector)

    selected, reason = planner.choose(
        context={"remaining_step_budget": 1},
        original_spec=_original_spec(),
        preserve_acquisition_filters=True,
        validate=lambda _spec: [],
        on_event=lambda *_args, **_kwargs: None,
    )

    assert selected is None
    assert "No proposed strategy" in reason
    assert proposer.calls == 1
    assert selector.calls == 0


def test_strategy_planner_reduces_unevidenced_deep_url_to_public_origin() -> None:
    deep = _candidate()
    deep["strategy"] = "Open a guessed site-specific forecast route"
    deep["actions"] = [{
        "name": "open_guessed_forecast",
        "capability": "open_url",
        "fixed_args": {"url": "https://weather.example/city/guessed-id"},
    }]
    proposer = _JsonModel({"candidates": [deep]})
    selector = _JsonModel({
        "decision": "attempt", "chosen_index": 0, "reason": "Grounded public origin",
    })
    events = []

    selected, _reason = StrategyPlanner(proposer, selector=selector).choose(
        context={"remaining_step_budget": 4},
        original_spec=_original_spec(),
        preserve_acquisition_filters=True,
        validate=lambda _spec: [],
        on_event=lambda event, **payload: events.append((event, payload)),
    )

    assert selected is not None
    assert selected.actions[0].fixed_args["url"] == "https://weather.example/"
    assert events[0][1]["candidates"][0]["diagnostics"] == []


def test_strategy_selector_receives_lower_cost_candidates_first() -> None:
    slower = _candidate(estimated_steps=5)
    slower["strategy"] = "Use a longer unfamiliar path"
    faster = _candidate(estimated_steps=3)
    faster["strategy"] = "Use a shorter unfamiliar path"
    proposer = _JsonModel({"candidates": [slower, faster]})
    selector = _JsonModel({
        "decision": "attempt", "chosen_index": 0, "reason": "Lowest cost",
    })
    events = []

    selected, _reason = StrategyPlanner(proposer, selector=selector).choose(
        context={"remaining_step_budget": 7},
        original_spec=_original_spec(),
        preserve_acquisition_filters=True,
        validate=lambda _spec: [],
        on_event=lambda event, **payload: events.append((event, payload)),
    )

    assert selected is not None
    assert selected.strategy == "Use a shorter unfamiliar path"
    assert events[1][1]["chosen_index"] == 0
    assert events[1][1]["chosen_proposal_index"] == 1
