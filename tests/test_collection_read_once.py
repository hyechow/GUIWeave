"""Regression tests for one-shot collection statements."""

from __future__ import annotations

import io

from PIL import Image, ImageDraw

from gui_agent.core.schemas import StatementContract, Observation
from gui_agent.core.supervisor.statement import StatementSupervisorPolicy
from gui_agent.core.supervisor.statement.schemas import (
    _StatementTransitionResult,
    _TransitionEvidence,
)


def _png() -> bytes:
    buf = io.BytesIO()
    img = Image.new("RGB", (120, 120), (240, 240, 240))
    draw = ImageDraw.Draw(img)
    draw.rectangle((10, 10, 110, 35), fill=(80, 80, 80))
    draw.rectangle((10, 45, 110, 105), outline=(40, 40, 40), width=2)
    draw.text((18, 58), "6 records", fill=(10, 10, 10))
    img.save(buf, format="PNG")
    return buf.getvalue()


def _policy(task_type: str = "analysis") -> tuple[StatementSupervisorPolicy, StatementContract]:
    policy = StatementSupervisorPolicy()
    statement = StatementContract(
        id="1",
        name="Read result count",
        description="Read the count displayed on the current page.",
        success_condition="The result count is visible.",
        kind="collection",
        completion_strategy="read_once",
    )
    policy.begin_statement(statement, instance_id="test:collection")
    policy.task_type = task_type  # type: ignore[assignment]
    return policy, statement


def test_done_read_once_collection_requests_reader():
    policy, statement = _policy("analysis")
    policy._invoke_statement_transition = lambda *a, **k: _StatementTransitionResult(  # type: ignore[assignment]
        kind="complete",
        reason="The result count is visible.",
        summary="The page shows a result count.",
        page_identity="Result list",
        read_instruction="Extract the visible result count.",
        evidence=[_TransitionEvidence(
            source="current_observation",
            claim="The result count is visible.",
        )],
    )

    step = policy._run_single_turn(statement, Observation(png_bytes=_png(), source="eval"), [])

    assert step.outcome is not None and step.outcome.phase == "completed"
    assert step.read_instruction == "Extract the visible result count."
    assert step.allow_read is True


def test_action_task_done_collection_does_not_collect_notes():
    policy, statement = _policy("action")
    policy._invoke_statement_transition = lambda *a, **k: _StatementTransitionResult(  # type: ignore[assignment]
        kind="complete",
        reason="The action result is visible.",
        summary="The page shows the target state.",
        page_identity="Result list",
        read_instruction="Extract the visible result count.",
        evidence=[_TransitionEvidence(
            source="current_observation",
            claim="The action result is visible.",
        )],
    )

    step = policy._run_single_turn(statement, Observation(png_bytes=_png(), source="eval"), [])

    assert step.outcome is not None and step.outcome.phase == "completed"
    assert step.read_instruction is None
    assert step.allow_read is False
