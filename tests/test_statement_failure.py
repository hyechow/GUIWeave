"""A model-declared blockage is diagnostic, not a Program terminal."""

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


def test_model_declared_failure_keeps_statement_running(monkeypatch) -> None:
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
            form_controls_meta={"coverage": "complete", "truncated": False},
        ),
        [],
    )

    assert step.outcome is None
    assert step.retry_transition is True
    assert "not a terminal runtime fact" in step.summary
    assert any("恢复、导航或继续观察" in item for item in policy.constraints_snapshot())

    policy.end_statement()
    assert policy.constraints_snapshot() == []


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
