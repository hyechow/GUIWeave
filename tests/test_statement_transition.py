from pydantic import ValidationError
import pytest

from gui_agent.core.run.statement_transition import validate_evidence_references
from gui_agent.core.supervisor.statement.schemas import (
    _StatementTransitionResult,
    _TransitionAction,
    _TransitionAssessment,
    _TransitionEvidence,
)


def _assessment(status, gaps=None):
    return _TransitionAssessment(
        status=status,
        summary="current statement state",
        open_gaps=list(gaps or []),
    )


def test_act_transition_requires_one_explicit_where_and_what_action():
    result = _StatementTransitionResult(
        assessment=_assessment("in_progress", ["the requested value is not saved"]),
        kind="act",
        reason="continue",
        action=_TransitionAction(
            instruction="In the visible Size section, enter 31 in the Value field",
            action_family="input",
            target_control="Size / Value field",
            target_value="31",
            expected_result="the Size section displays value 31",
        ),
    )
    assert result.action.target_control == "Size / Value field"
    assert result.action.target_value == "31"


def test_iterate_family_has_one_canonical_atomic_role():
    action = _TransitionAction(
        instruction="Scroll down until Configurations is visible",
        atomic_role="prepare",
        action_family="iterate",
        target_control="Configurations",
        expected_result="Configurations becomes visible",
    )

    assert action.atomic_role == "iterate"


def test_transition_schema_does_not_offer_stop_or_no_action_kinds():
    with pytest.raises(ValidationError):
        _StatementTransitionResult.model_validate(
            {
                "assessment": {
                    "status": "in_progress",
                    "summary": "continue",
                    "open_gaps": ["not done"],
                },
                "kind": "stop",
                "reason": "stop",
            }
        )


def test_complete_needs_satisfied_assessment_and_cited_evidence():
    with pytest.raises(ValidationError):
        _StatementTransitionResult(
            assessment=_assessment("satisfied"),
            kind="complete",
            reason="done",
        )

    result = _StatementTransitionResult(
        assessment=_assessment("satisfied"),
        kind="complete",
        reason="done",
        evidence=[
            _TransitionEvidence(
                source="current_observation",
                claim="the requested values and save confirmation are visible",
            )
        ],
    )
    assert result.kind == "complete"


def test_evidence_validation_rejects_invented_journal_references():
    evidence = [
        _TransitionEvidence(
            source="journal",
            event_ref="turn:9",
            claim="the save action was dispatched",
        )
    ]
    denied = validate_evidence_references(evidence, available_refs={"turn:8"})
    allowed = validate_evidence_references(evidence, available_refs={"turn:9"})
    assert denied.allowed is False
    assert "turn:9" in denied.reason
    assert allowed.allowed is True


def test_failed_is_not_an_action_and_requires_evidence():
    result = _StatementTransitionResult(
        assessment=_assessment("blocked"),
        kind="failed",
        reason="the required entity does not exist",
        evidence=[
            _TransitionEvidence(
                source="current_observation",
                claim="the authoritative result is empty",
            )
        ],
    )
    assert result.action is None
