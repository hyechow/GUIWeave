"""Structured action-lifecycle protocol for milestone execution.

The module intentionally contains no task vocabulary or instruction-text classifiers.  Planner
metadata records the action role, while executed turns provide dispatch and target receipts.
"""

from __future__ import annotations

from typing import Iterable, Protocol

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
    """Return coherent structured metadata for this execution contract.

    A persisted mutation's terminal boundary is represented by both ``atomic_role=commit`` and
    ``action_family=commit``.  Nested wizard actions remain executable, but a conflicting
    ``commit + activate`` proposal cannot consume the outer resource's persistence boundary.
    """
    if milestone.is_iterative:
        return "iterate", "iterate"
    if milestone.requires_commit:
        if plan.action_family == "commit":
            return "commit", "commit"
        if plan.atomic_role == "commit":
            return "prepare", plan.action_family
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
    if any(is_commit_turn(turn, milestone) for turn in history):
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
            and _same_surface(signal.surface_id, current_surface_id)
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


def parent_persistence_pending(
    milestone: Milestone,
    history: list[PolicyTurn],
    available_targets: Iterable[str],
    *,
    current_surface_id: str = "",
) -> bool:
    """Whether a completed child-edit flow has returned to its parent persistence surface.

    A reusable parent-entry control is distinguished from an in-flow control without vocabulary:
    it was traversed before the first write, was not traversed again while advancing after that
    write, and is present again in the current observation.  Repeated wizard controls therefore do
    not look like a parent return.  The parent can only be pending when the milestone explicitly
    declares a separate commit boundary and no commit has crossed it yet.
    """
    if not milestone.requires_commit:
        return False
    turns = [
        turn
        for turn in history
        if turn.supervisor is not None
        and turn.supervisor.milestone_id == milestone.id
        and _responded_on_target(turn)
    ]
    if any(is_commit_turn(turn, milestone) for turn in turns):
        return False
    first_write = next(
        (index for index, turn in enumerate(turns) if turn.action_signal.role == "write"),
        None,
    )
    if first_write is None:
        return False
    after_write = turns[first_write + 1 :]
    if not any(turn.action_signal.role == "prepare" for turn in after_write):
        return False

    later_prepare_targets = {
        _control_key(turn.action_signal.target_control)
        for turn in after_write
        if turn.action_signal.role == "prepare"
    }
    rendered = {_control_key(str(key)) for key in available_targets if _control_key(str(key))}
    if rendered.intersection(later_prepare_targets):
        return False
    for turn in turns[:first_write]:
        signal = turn.action_signal
        target = _control_key(signal.target_control)
        if (
            signal.role == "prepare"
            and target
            and _same_surface(signal.surface_id, current_surface_id)
            and target not in later_prepare_targets
            and target in rendered
        ):
            return True
    return False


def concrete_persistence_plan(
    plan: PlannedAction,
    milestone: Milestone,
    available_targets: Iterable[str],
) -> bool:
    """Whether a commit proposal names a concrete target on the active surface."""
    role, family = action_metadata(plan, milestone)
    target = _control_key(str(getattr(plan, "target_control", "") or ""))
    rendered = {_control_key(str(key)) for key in available_targets if _control_key(str(key))}
    return bool(
        role == "commit"
        and family == "commit"
        and target
        and target in rendered
    )


def _responded_on_target(turn: PolicyTurn) -> bool:
    signal = turn.action_signal
    return bool(
        turn.executed
        and signal is not None
        and signal.execution == "dispatched"
        and signal.target != "off_target"
        and signal.response == "observed"
        and signal.outcome != "contradicted"
    )


def _same_surface(recorded: str, current: str) -> bool:
    """Missing identities retain legacy fallback; two known identities must match exactly."""
    return not recorded or not current or recorded == current


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
    "concrete_persistence_plan",
    "is_commit_turn",
    "parent_persistence_pending",
    "regresses_preparation_frontier",
    "record_action_outcome",
    "record_action_response",
]
