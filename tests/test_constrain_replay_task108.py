"""Replay the task-108 frame without reviving label-based authorization.

The retained observation predates structural effect tags. It contains a button
labelled ``Apply Filters`` but not the DOM ancestry that proves the button is a
query-control effect. The new gate must therefore fail closed. Live browser
classification from filter-container ancestry is covered separately by
``test_browser_form_reader``.
"""

from __future__ import annotations

import json
from pathlib import Path

from gui_agent.adapters.browser.actions import BrowserAction, BrowserActionDecision
from gui_agent.adapters.browser.policies import BrowserActionPolicy
from gui_agent.core.filter_contract import compile_filter_predicates
from gui_agent.core.schemas import (
    ActionIntent,
    CollectionIntent,
    Observation,
    StatementContract,
    SupervisorStep,
)
from gui_agent.core.supervisor.statement.observation_view import build_observation_view
from gui_agent.core.supervisor.statement.policy import StatementSupervisorPolicy

_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "replay/fixtures/browser/194421_constrain_filter/observation_turn_5.json"
)


def _recorded_observation() -> Observation:
    payload = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    return Observation.model_validate({**payload["observation"], "png_bytes": b"x"})


def _contract() -> StatementContract:
    return StatementContract(
        id="c1",
        goal="Narrow the Orders collection",
        success="The exact Status predicate is active",
        interaction_intent=CollectionIntent(
            phase="constrain",
            entity="Orders",
            predicates=compile_filter_predicates({"Status": "Complete"}),
        ),
    )


def test_recorded_label_does_not_grant_query_effect_permission() -> None:
    observation = _recorded_observation()
    contract = _contract()
    view = build_observation_view(contract, observation, [])
    apply = next(
        item for item in view.affordances
        if str(item.get("label") or "").strip().casefold() == "apply filters"
    )
    assert "activate" in (apply.get("supported_operations") or [])
    assert apply.get("effect_kind") is None

    step = SupervisorStep(
        action_intent=ActionIntent(
            instruction="activate the grounded filter submission control",
            family="activate",
            target_control="Apply Filters",
            target_ref=str(apply["ref"]),
        ),
        summary="submit filter",
    )
    decision = BrowserActionDecision(
        action=BrowserAction(
            action_type="tap",
            x=500,
            y=500,
            description="activate grounded control",
        )
    )
    effect = BrowserActionPolicy().resolve_action_effect(
        step,
        observation,
        decision,
    )

    policy = StatementSupervisorPolicy()
    policy.begin_statement(contract, instance_id="i1:c1")
    rejection = policy.authorize_grounded_action(effect)

    assert effect == "unknown"
    assert rejection
