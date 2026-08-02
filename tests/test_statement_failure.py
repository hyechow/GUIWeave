"""A model-declared blockage is diagnostic, not a Program terminal."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from gui_agent.core.schemas import Observation, StatementContract
from gui_agent.core.supervisor.statement import policy as policy_module
from gui_agent.core.supervisor.statement.policy import StatementSupervisorPolicy
from gui_agent.core.supervisor.statement.schemas import (
    _StatementTransitionResult,
    _TransitionAction,
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
    assert any("模型自己的判断" in item for item in policy.constraints_snapshot())

    policy.end_statement()
    assert policy.constraints_snapshot() == []


def test_failed_recovery_path_is_reissued_as_an_action_same_frame(monkeypatch) -> None:
    statement = StatementContract(
        id="m1",
        goal="restore the parent collection",
        success="the parent collection is visible",
    )
    policy = StatementSupervisorPolicy()
    policy.begin_statement(statement, instance_id="i1")
    monkeypatch.setattr(policy_module, "is_loading_frame", lambda _obs: False)
    calls = 0

    def transition(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _StatementTransitionResult(
                assessment=_TransitionAssessment(
                    status="blocked",
                    summary="the child view must be exited with system Back",
                    established_facts=["the child view is active"],
                ),
                kind="failed",
                reason="use system Back to return to the parent collection",
                evidence=[_TransitionEvidence(
                    source="current_observation",
                    claim="the child collection is visible",
                )],
            )
        assert any(
            "use system Back to return" in item
            for item in policy.constraints_snapshot()
        )
        return _StatementTransitionResult(
            assessment=_TransitionAssessment(
                status="in_progress",
                summary="return to the parent collection",
                established_facts=["the child view is active"],
                open_gaps=["the parent collection is not visible"],
            ),
            kind="act",
            reason="the identified recovery path is executable",
            action=_TransitionAction(
                instruction="Use system Back to return to the parent collection",
                atomic_role="prepare",
                action_family="navigate",
                target_control="Back",
                expected_result="the parent collection becomes visible",
            ),
        )

    monkeypatch.setattr(policy, "_invoke_statement_transition", transition)

    step = policy._run_single_turn(
        statement,
        Observation(png_bytes=b"x", source="android"),
        [],
    )

    assert calls == 2
    assert step.action_intent is not None
    assert step.action_intent.family == "navigate"
    assert step.action_intent.target_control == "Back"


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
