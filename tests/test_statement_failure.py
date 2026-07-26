"""Terminal failure is owned by Transition and requires cited evidence."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from gui_agent.core.schemas import Observation, StatementContract
from gui_agent.core.supervisor.statement import policy as policy_module
from gui_agent.core.supervisor.statement.policy import StatementSupervisorPolicy
from gui_agent.core.supervisor.statement.schemas import (
    _StatementTransitionResult,
    _TransitionAssessment,
    _TransitionEvidence,
)


def _decision() -> _StatementTransitionResult:
    return _StatementTransitionResult(
        assessment=_TransitionAssessment(
            status="blocked",
            summary="no viable control is present",
            established_facts=["the current inventory contains no usable target"],
        ),
        kind="failed",
        reason="the Statement has no viable action on this surface",
        evidence=[_TransitionEvidence(
            source="current_observation",
            claim="the current control inventory has no usable target",
        )],
    )


def test_transition_failure_is_not_reclassified_by_inventory_heuristics(monkeypatch) -> None:
    statement = StatementContract(
        id="m1",
        goal="set rating",
        success="Rating<=3 is applied",
    )
    policy = StatementSupervisorPolicy()
    policy.begin_statement(statement, instance_id="i1")
    monkeypatch.setattr(policy_module, "is_loading_frame", lambda _obs: False)
    monkeypatch.setattr(policy, "_invoke_statement_transition", lambda *a, **k: _decision())

    step = policy._run_single_turn(
        statement,
        Observation(
            png_bytes=b"x",
            source="browser",
            form_controls_meta={"coverage": "partial", "truncated": True},
        ),
        [],
    )

    assert step.outcome is not None and step.outcome.phase == "failed"
    assert "no viable action" in step.outcome.summary
    assert policy._last_transition_record["validation_error"] == ""


def test_failed_requires_evidence_in_schema() -> None:
    with pytest.raises(ValidationError, match="requires cited evidence"):
        _StatementTransitionResult(
            assessment=_TransitionAssessment(
                status="blocked",
                summary="blocked",
            ),
            kind="failed",
            reason="blocked",
        )
