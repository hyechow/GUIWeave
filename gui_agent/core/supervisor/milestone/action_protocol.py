"""Structured action-lifecycle protocol for milestone execution.

The module intentionally contains no task vocabulary or instruction-text classifiers.  Planner
metadata records the action role, while executed turns provide dispatch and target receipts.
"""

from __future__ import annotations

from typing import Literal, Protocol

from gui_agent.core.run.action_ledger import ActionLedger
from gui_agent.core.run.execution_signals import CompletionEvaluation
from gui_agent.core.run.progress_monitor import ProgressMonitor
from gui_agent.core.schemas import ActionFamily, AtomicRole, Milestone, PolicyTurn


class PlannedAction(Protocol):
    atomic_role: AtomicRole
    action_family: ActionFamily


SurfaceRelation = Literal["same", "different", "unknown"]


def action_metadata(
    plan: PlannedAction,
    milestone: Milestone,
) -> tuple[AtomicRole, ActionFamily]:
    """Return the planner's independent transaction role and UI primitive family.

    A Save button is normally ``commit + activate``; the primitive family is not a persistence
    signal.
    """
    if milestone.is_iterative:
        return "iterate", "iterate"
    return plan.atomic_role, plan.action_family


def regresses_preparation_frontier(
    plan: PlannedAction,
    milestone: Milestone,
    history: list[PolicyTurn],
    *,
    current_surface_id: str = "",
) -> bool:
    """Whether a proposal re-enters an already completed preparation surface.

    Persistent mutations are monotonic transactions: once an on-target preparation has responded
    and later actions have advanced the same execution scope, revisiting that earlier control is a
    backward edge.  Explicit outcome contradiction is handled by the caller and may permit retry.
    """
    role, family = action_metadata(plan, milestone)
    target = _control_key(getattr(plan, "target_control", ""))
    if not milestone.requires_commit or role != "prepare" or family != "activate" or not target:
        return False
    completed_at = -1
    for index, turn in enumerate(history):
        signal = turn.action_signal
        if (
            signal is not None
            and turn.supervisor is not None
            and turn.supervisor.milestone_id == milestone.id
            and signal.role == "prepare"
            and signal.execution == "dispatched"
            and signal.target != "off_target"
            and signal.response == "observed"
            and signal.outcome != "contradicted"
            and _surface_relation(signal.surface_id, current_surface_id) == "same"
            and _control_key(signal.target_control) == target
        ):
            completed_at = index
    return completed_at >= 0 and any(
        turn.action_signal is not None
        and turn.supervisor is not None
        and turn.supervisor.milestone_id == milestone.id
        and turn.action_signal.execution == "dispatched"
        and turn.action_signal.target != "off_target"
        and turn.action_signal.response == "observed"
        and _control_key(turn.action_signal.target_control) != target
        for turn in history[completed_at + 1 :]
    )


def _surface_relation(recorded: str, current: str) -> SurfaceRelation:
    """Compare two optional surface identities without treating absence as equality."""
    if not recorded or not current:
        return "unknown"
    return "same" if recorded == current else "different"


def _control_key(value: str) -> str:
    return "".join(char.casefold() for char in (value or "") if char.isalnum())


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
    "regresses_preparation_frontier",
    "record_action_outcome",
    "record_action_response",
]
