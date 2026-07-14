"""Structured action-lifecycle protocol for milestone execution.

The module intentionally contains no task vocabulary or instruction-text classifiers.  Planner
metadata records the action role, while executed turns provide dispatch and target receipts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from gui_agent.core.run.action_ledger import ActionLedger
from gui_agent.core.run.execution_signals import CompletionEvaluation
from gui_agent.core.run.progress_monitor import ProgressMonitor
from gui_agent.core.schemas import ActionFamily, ActionSignal, AtomicRole, Milestone, PolicyTurn


class PlannedAction(Protocol):
    atomic_role: AtomicRole
    action_family: ActionFamily


MutationPhase = Literal[
    "preparing", "written", "commit_pending", "terminal", "contradicted"
]


@dataclass(frozen=True)
class MutationProgress:
    """Read-only lifecycle projection shared by evidence and proposal validation."""

    phase: MutationPhase
    latest_dispatch: PolicyTurn | None = None
    latest_write: PolicyTurn | None = None
    terminal_index: int | None = None
    entry_surface: str = ""
    closed_preparations: frozenset[tuple[str, str]] = frozenset()

    def rejects(
        self,
        plan: PlannedAction,
        milestone: Milestone,
        *,
        current_surface_id: str = "",
    ) -> bool:
        """Whether a proposal moves backward from the derived mutation progress."""
        role, family = action_metadata(plan, milestone)
        target = _control_key(getattr(plan, "target_control", ""))
        signal = self.latest_dispatch.action_signal if self.latest_dispatch else None
        terminal_only = bool(
            self.phase == "commit_pending"
            and signal is not None
            and signal.role == "commit"
            and self.entry_surface
            and current_surface_id == self.entry_surface
        )
        return bool(
            milestone.requires_commit
            and self.phase != "contradicted"
            and (
                (terminal_only and role != "commit")
                or (
                    role == "prepare"
                    and family == "activate"
                    and target
                    and current_surface_id
                    and (current_surface_id, target) in self.closed_preparations
                )
            )
        )


def action_metadata(
    plan: PlannedAction,
    milestone: Milestone,
) -> tuple[AtomicRole, ActionFamily]:
    """Return the planner's independent lifecycle role and UI primitive family.

    A Save button is normally ``commit + activate``; the primitive family is not a persistence
    signal.
    """
    if milestone.is_iterative:
        return "iterate", "iterate"
    return plan.atomic_role, plan.action_family


def mutation_progress(
    milestone: Milestone,
    history: list[PolicyTurn],
    *,
    scope: str = "",
) -> MutationProgress:
    """Project dispatch history; surface identity is optional boundary evidence."""
    events = [
        (turn, signal)
        for turn in history
        if turn.supervisor.milestone_id == milestone.id
        and (not scope or turn.supervisor.execution_scope in {"", scope})
        and (signal := turn.action_signal) is not None
        and signal.execution == "dispatched"
    ]

    latest_dispatch = events[-1][0] if events else None
    valid_events = [
        (turn, signal)
        for turn, signal in events
        if signal.target != "off_target"
        and (turn.target_verify is None or turn.target_verify.on_target)
    ]
    entry_surface = next(
        (signal.surface_id for _, signal in valid_events if signal.surface_id),
        "",
    )

    terminal_index = next(
        (
            turn.index
            for turn, signal in reversed(valid_events)
            if _is_terminal_boundary(signal, entry_surface)
        ),
        None,
    )
    receipt_required = bool(milestone.kind == "action" and milestone.target_values)
    latest_write = next(
        (
            turn
            for turn, signal in reversed(valid_events)
            if (
                signal.mutation_receipt is not None
                and signal.mutation_receipt.statement_id == milestone.id
                if receipt_required
                else signal.role == "write"
            )
        ),
        None,
    )

    open_preparations: set[tuple[str, str]] = set()
    closed_preparations: set[tuple[str, str]] = set()
    for _, signal in valid_events:
        if (
            signal.response != "observed"
            or signal.outcome == "contradicted"
            or not (target := _control_key(signal.target_control))
        ):
            continue
        closed_preparations.update(
            item for item in open_preparations if item[1] != target
        )
        if signal.role == "prepare" and signal.surface_id:
            open_preparations.add((signal.surface_id, target))

    latest_signal = latest_dispatch.action_signal if latest_dispatch is not None else None
    phase: MutationPhase = (
        "commit_pending" if latest_write is not None and milestone.requires_commit
        else "written" if latest_write is not None else "preparing"
    )
    if terminal_index is not None:
        phase = "terminal"
    if latest_signal is not None and latest_signal.outcome == "contradicted":
        phase = "contradicted"
    return MutationProgress(
        phase=phase,
        latest_dispatch=latest_dispatch,
        latest_write=latest_write,
        terminal_index=terminal_index,
        entry_surface=entry_surface,
        closed_preparations=frozenset(closed_preparations),
    )


def _control_key(value: str) -> str:
    return "".join(char.casefold() for char in (value or "") if char.isalnum())


def _is_terminal_boundary(signal: ActionSignal, entry_surface: str) -> bool:
    """Whether one concrete dispatch can represent the statement persistence boundary."""
    return bool(
        signal.role == "commit"
        and (
            not entry_surface
            or not signal.surface_id
            or signal.surface_id == entry_surface
            or "url" in signal.response_channels
        )
    )


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
    negative = bool(
        decision.status == "contradicted"
        and any(
            item.value == "contradicted"
            and item.authoritative
            and item.domain in {"action.target", "business.outcome"}
            for item in decision.used_claims
        )
    )
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
    "MutationProgress",
    "action_metadata",
    "mutation_progress",
    "record_action_outcome",
    "record_action_response",
]
