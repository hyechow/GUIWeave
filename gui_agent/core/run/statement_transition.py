"""Hard validation for Agentic Statement Transition proposals.

This module never chooses an action, recovery route, or fallback kind.  It only
validates evidence/contract boundaries and reports a rejection reason to the LLM.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal, Protocol

from gui_agent.core.orchestrator.recovery import DEAD_ROUTE_MARKER, REQUIRED_ROUTE_MARKER
from gui_agent.core.run.execution_signals import CompletionEvaluation


class TransitionEvidenceLike(Protocol):
    source: str
    event_ref: str
    claim: str


@dataclass(frozen=True)
class GuardVerdict:
    allowed: bool
    reason: str
    verification: Literal["confirmed", "accepted_unverified", ""] = ""


def guard_complete(
    evaluation: CompletionEvaluation,
) -> GuardVerdict:
    """Validate a completed outcome and expose Runtime's evidence grade.

    Verification is never negotiated with the model. If evidence supports only
    ``accepted_unverified``, that is the outcome grade; it is not a reason to spend another
    LLM call restating the same terminal proposal.
    """
    if evaluation.status == "satisfied" and evaluation.completion_status in {
        "confirmed",
        "accepted_unverified",
    }:
        return GuardVerdict(
            allowed=True,
            reason=evaluation.reason,
            verification=evaluation.completion_status,
        )
    return GuardVerdict(
        allowed=False,
        reason=evaluation.reason or "completion evidence insufficient",
    )


def guard_evidence_references(
    evidence: Iterable[TransitionEvidenceLike],
    *,
    available_refs: set[str],
) -> GuardVerdict:
    """Reject invented Journal references while allowing current-frame descriptions."""
    items = list(evidence)
    if not items:
        return GuardVerdict(False, "transition decision did not cite evidence")
    invalid = sorted({
        item.event_ref
        for item in items
        if item.source == "journal" and item.event_ref not in available_refs
    })
    if invalid:
        return GuardVerdict(
            False,
            "transition cited Journal events absent from StatementMemory: "
            + ", ".join(invalid),
        )
    if any(not str(item.claim or "").strip() for item in items):
        return GuardVerdict(False, "transition evidence contains an empty claim")
    return GuardVerdict(True, "transition evidence references are valid")


def guard_infeasible(
    *,
    evidence_valid: bool,
    structure_complete: bool,
    reason: str,
    kickback: str = "",
) -> GuardVerdict:
    """Allow infeasible only when the current structural inventory proves absence."""
    if not evidence_valid:
        return GuardVerdict(False, "infeasible transition lacks valid evidence")
    if not structure_complete:
        return GuardVerdict(
            False,
            "current control inventory is incomplete; infeasible is not proven",
        )
    if (
        DEAD_ROUTE_MARKER not in kickback
        or REQUIRED_ROUTE_MARKER not in kickback
    ):
        return GuardVerdict(
            False,
            "infeasible transition requires typed dead-route and required-route markers",
        )
    return GuardVerdict(True, reason or "statement infeasible")


__all__ = [
    "GuardVerdict",
    "guard_complete",
    "guard_evidence_references",
    "guard_infeasible",
]
