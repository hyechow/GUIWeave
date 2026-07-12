"""Structured action-lifecycle protocol for milestone execution.

The module intentionally contains no task vocabulary or instruction-text classifiers.  Planner
metadata records the action role, while executed turns provide dispatch and target receipts.
"""

from __future__ import annotations

from typing import Protocol

from gui_agent.core.run.action_ledger import ActionLedger
from gui_agent.core.run.execution_signals import CompletionEvaluation
from gui_agent.core.run.progress_monitor import ProgressMonitor
from gui_agent.core.schemas import ActionFamily, AtomicRole, Milestone, PolicyTurn


class PlannedAction(Protocol):
    atomic_role: AtomicRole
    action_family: ActionFamily


def action_metadata(
    plan: PlannedAction,
    milestone: Milestone,
) -> tuple[AtomicRole, ActionFamily]:
    """Return structured planner metadata with only the iterative contract override."""
    if milestone.is_iterative:
        return "iterate", "iterate"
    return plan.atomic_role, plan.action_family


def is_commit_turn(turn: PolicyTurn | None, milestone: Milestone) -> bool:
    """Whether an on-target executed turn crossed the declared persistence boundary."""
    if turn is None or not turn.executed:
        return False
    supervisor = turn.supervisor
    if supervisor is None or supervisor.milestone_id != milestone.id:
        return False
    if supervisor.atomic_role != "commit":
        return False
    target_verify = turn.target_verify
    return target_verify is None or target_verify.on_target


def milestone_commit_succeeded(
    history: list[PolicyTurn], milestone: Milestone
) -> bool:
    return any(is_commit_turn(turn, milestone) for turn in history)


def record_action_response(
    history: list[PolicyTurn],
    milestone: Milestone,
    *,
    monitor: ProgressMonitor,
    ledger: ActionLedger,
) -> None:
    """Attach deterministic page-response evidence to the latest unresolved dispatch."""
    turn = ledger.latest_pending(history, milestone.id)
    if turn is None or turn.action_signal is None:
        return
    signal = turn.action_signal
    if monitor.url_changed:
        signal.response = "observed"
        if "url" not in signal.response_channels:
            signal.response_channels.append("url")
    if monitor.dom_changed:
        signal.response = "observed"
        if "dom" not in signal.response_channels:
            signal.response_channels.append("dom")
    if turn.no_effect and signal.response == "unknown":
        signal.response = "none_observed"


def record_action_outcome(
    history: list[PolicyTurn],
    milestone: Milestone,
    decision: CompletionEvaluation,
    *,
    ledger: ActionLedger,
) -> None:
    """Persist only an arbitrated completion result for the latest dispatch."""
    turn = ledger.latest_pending(history, milestone.id)
    if turn is None or turn.action_signal is None:
        return
    signal = turn.action_signal
    negative = decision.status == "contradicted"
    if negative:
        signal.outcome = "contradicted"
    elif (
        decision.status == "satisfied"
        and decision.completion_status == "confirmed"
        and signal.outcome != "contradicted"
    ):
        signal.outcome = "confirmed"
    if negative or signal.outcome == "confirmed":
        evidence = [
            *(item.evidence for item in decision.used_claims if item.evidence),
            decision.reason,
        ]
        for item in evidence:
            if item and item not in signal.outcome_evidence:
                signal.outcome_evidence.append(item)


__all__ = [
    "action_metadata",
    "is_commit_turn",
    "milestone_commit_succeeded",
    "record_action_outcome",
    "record_action_response",
]
