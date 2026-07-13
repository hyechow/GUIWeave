"""Structured action-lifecycle protocol for milestone execution.

The module intentionally contains no task vocabulary or instruction-text classifiers.  Planner
metadata records the action role, while executed turns provide dispatch and target receipts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal, Protocol

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

    A Save button is normally ``commit + activate``. Whether its receipt crossed the terminal
    persistence boundary is resolved from surface history by ``persistence_boundary_state``;
    the primitive family is not a persistence signal.
    """
    if milestone.is_iterative:
        return "iterate", "iterate"
    return plan.atomic_role, plan.action_family


@dataclass(frozen=True)
class PersistenceBoundaryState:
    """Single interpretation of the current persistence frontier."""

    parent_pending: bool = False
    reason: str = ""

    def is_terminal_dispatch(
        self,
        turn: PolicyTurn | None,
        milestone: Milestone,
    ) -> bool:
        return bool(is_commit_turn(turn, milestone) and not self.parent_pending)

    def accepts_parent_plan(
        self,
        plan: PlannedAction,
        milestone: Milestone,
        available_targets: Iterable[str],
    ) -> bool:
        """Whether a pending parent boundary is bound to a concrete current control."""
        if not self.parent_pending:
            return False
        role, family = action_metadata(plan, milestone)
        target = _control_key(str(getattr(plan, "target_control", "") or ""))
        rendered = {
            _control_key(str(key))
            for key in available_targets
            if _control_key(str(key))
        }
        return bool(
            role == "commit"
            and family not in {"unknown", "iterate"}
            and target
            and target in rendered
        )


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


def _parent_persistence_pending(
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
    # A commit on the active surface already crossed this boundary. A commit on a departed child
    # surface is provisional: returning to a known parent entry proves that more persistence work
    # remains on the parent.
    for turn in turns:
        if not is_commit_turn(turn, milestone):
            continue
        signal = turn.action_signal
        if signal is None:
            continue
        relation = _surface_relation(signal.surface_id, current_surface_id)
        if relation == "unknown":
            return False
        if relation == "same":
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
            and _surface_relation(signal.surface_id, current_surface_id) == "same"
            and target not in later_prepare_targets
            and target in rendered
        ):
            return True
    return False


def persistence_boundary_state(
    milestone: Milestone,
    history: list[PolicyTurn],
    available_targets: Iterable[str],
    *,
    current_surface_id: str = "",
) -> PersistenceBoundaryState:
    """Resolve parent-return and terminal-dispatch semantics for all consumers."""
    pending = _parent_persistence_pending(
        milestone,
        history,
        available_targets,
        current_surface_id=current_surface_id,
    )
    return PersistenceBoundaryState(
        parent_pending=pending,
        reason=(
            "child-surface dispatch returned draft state to a parent persistence surface"
            if pending
            else "no uncommitted parent persistence surface is proven"
        ),
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
    "PersistenceBoundaryState",
    "action_metadata",
    "is_commit_turn",
    "persistence_boundary_state",
    "regresses_preparation_frontier",
    "record_action_outcome",
    "record_action_response",
]
