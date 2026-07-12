"""Structured action-lifecycle protocol for milestone execution.

The module intentionally contains no task vocabulary or instruction-text classifiers.  Planner
metadata records the action role, while executed turns provide dispatch and target receipts.
"""

from __future__ import annotations

from typing import Protocol

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


__all__ = ["action_metadata", "is_commit_turn", "milestone_commit_succeeded"]
