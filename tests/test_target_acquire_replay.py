from __future__ import annotations

import json
from pathlib import Path

from gui_agent.core.schemas import PolicyContext
from gui_agent.core.supervisor.milestone.acquisition import TargetAcquireController
from scripts.replay_supervisor_turn import _milestone_for_turn, normalize_replay_context


FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "evals/browser/supervisor_replay/170119_target_acquire"
)


def _controls(turn: int) -> list[dict]:
    raw = json.loads((FIXTURE / f"observation_turn_{turn}.json").read_text())
    return raw["observation"]["form_controls"]


def test_real_170119_frames_keep_one_target_directed_acquire_session() -> None:
    raw = normalize_replay_context(json.loads((FIXTURE / "context.json").read_text()))
    context = PolicyContext.model_validate(raw)
    turn = next(item for item in context.turns if item.index == 24)
    milestone = _milestone_for_turn(raw, turn)

    assert milestone.target_controls == ["configurations_collection"]
    assert milestone.target_values == {"Color": "green", "Size": "XXXL"}

    controller = TargetAcquireController()
    first = controller.decide(_controls(23), milestone, scope="row:product-1492")
    second = controller.decide(_controls(24), milestone, scope="row:product-1492")
    third = controller.decide(_controls(25), milestone, scope="row:product-1492")

    assert [first.status, second.status, third.status] == [
        "act",
        "act",
        "act",
    ]
    assert first.target_labels == second.target_labels == third.target_labels == (
        "Configurations",
    )
    assert all(
        decision.plan and decision.plan.direction == "down"
        for decision in (first, second, third)
    )


def test_real_170119_no_progress_exhausts_instead_of_drifting_to_fields() -> None:
    raw = normalize_replay_context(json.loads((FIXTURE / "context.json").read_text()))
    context = PolicyContext.model_validate(raw)
    turn = next(item for item in context.turns if item.index == 24)
    milestone = _milestone_for_turn(raw, turn)
    controller = TargetAcquireController()

    controller.decide(_controls(23), milestone, scope="row:product-1492")
    controller.decide(_controls(24), milestone, scope="row:product-1492")
    controller.decide(_controls(25), milestone, scope="row:product-1492")
    exhausted = controller.decide(_controls(26), milestone, scope="row:product-1492")

    assert exhausted.status == "exhausted"
    assert exhausted.target_labels == ("Configurations",)
    assert "did not advance the bound surface" in exhausted.reason
