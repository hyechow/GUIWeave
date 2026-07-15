from __future__ import annotations

import json
from pathlib import Path

from gui_agent.core.schemas import StatementContract
from gui_agent.core.supervisor.milestone.acquisition import TargetAcquireController


FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "evals/browser/supervisor_replay/170119_target_acquire"
)


def _controls(turn: int) -> list[dict]:
    raw = json.loads((FIXTURE / f"observation_turn_{turn}.json").read_text())
    return raw["observation"]["form_controls"]


def _milestone() -> StatementContract:
    return StatementContract(
        id="s1",
        name="configure product options",
        description="",
        success_condition="the saved collection contains green and XXXL",
        kind="action",
        target_controls=["configurations_collection"],
        target_values={"Color": "green", "Size": "XXXL"},
    )


def test_real_170119_frames_keep_one_target_directed_acquire_session() -> None:
    milestone = _milestone()

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
    milestone = _milestone()
    controller = TargetAcquireController()

    controller.decide(_controls(23), milestone, scope="row:product-1492")
    controller.decide(_controls(24), milestone, scope="row:product-1492")
    controller.decide(_controls(25), milestone, scope="row:product-1492")
    exhausted = controller.decide(_controls(26), milestone, scope="row:product-1492")

    assert exhausted.status == "exhausted"
    assert exhausted.target_labels == ("Configurations",)
    assert "did not advance the bound surface" in exhausted.reason
