"""Mechanical validation for Agentic Statement Transition proposals.

This module never chooses an action, recovery route, or fallback kind.  It only
validates evidence/contract boundaries and reports a rejection reason to the LLM.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal, Protocol

from gui_agent.core.run.execution_signals import CompletionEvaluation


class TransitionEvidenceLike(Protocol):
    source: str
    event_ref: str
    claim: str


@dataclass(frozen=True)
class ValidationVerdict:
    allowed: bool
    reason: str
    verification: Literal["confirmed", "accepted_unverified", ""] = ""


def validate_completion(
    evaluation: CompletionEvaluation,
) -> ValidationVerdict:
    """Validate a completed outcome and expose Runtime's evidence grade.

    Verification is never negotiated with the model. If evidence supports only
    ``accepted_unverified``, that is the outcome grade; it is not a reason to spend another
    LLM call restating the same terminal proposal.
    """
    if evaluation.status == "satisfied" and evaluation.completion_status in {
        "confirmed",
        "accepted_unverified",
    }:
        return ValidationVerdict(
            allowed=True,
            reason=evaluation.reason,
            verification=evaluation.completion_status,
        )
    return ValidationVerdict(
        allowed=False,
        reason=evaluation.reason or "completion evidence insufficient",
    )


def validate_evidence_references(
    evidence: Iterable[TransitionEvidenceLike],
    *,
    available_refs: set[str],
) -> ValidationVerdict:
    """Reject invented Journal references while allowing current-frame descriptions."""
    items = list(evidence)
    if not items:
        return ValidationVerdict(False, "transition decision did not cite evidence")
    invalid = sorted({
        item.event_ref
        for item in items
        if item.source == "journal" and item.event_ref not in available_refs
    })
    if invalid:
        return ValidationVerdict(
            False,
            "transition cited Journal events absent from StatementMemory: "
            + ", ".join(invalid),
        )
    if any(not str(item.claim or "").strip() for item in items):
        return ValidationVerdict(False, "transition evidence contains an empty claim")
    return ValidationVerdict(True, "transition evidence references are valid")


__all__ = [
    "ValidationVerdict",
    "validate_completion",
    "validate_evidence_references",
]
