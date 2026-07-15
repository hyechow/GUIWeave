"""Projection of statement-local write and commit receipts.

Persistence answers only whether dispatched writes crossed their declared boundary. It does not
judge target values, propose actions, or advance milestones.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from gui_agent.core.schemas import StatementContract, MutationReceipt, PolicyTurn


PersistenceStatus = Literal["clean", "pending", "submitted"]


@dataclass(frozen=True)
class PersistenceAssessment:
    status: PersistenceStatus
    pending_receipts: tuple[MutationReceipt, ...] = ()
    latest_write: PolicyTurn | None = None
    terminal_turn: PolicyTurn | None = None
    entry_surface: str = ""
    orphan_commit: bool = False
    terminal_ready: bool = False


def assess_persistence(
    milestone: StatementContract,
    history: list[PolicyTurn],
    *,
    scope: str = "",
    current_surface: str = "",
) -> PersistenceAssessment:
    """Project immutable action history into one persistence assessment."""
    events = [
        turn
        for turn in history
        if turn.supervisor is not None
        and turn.supervisor.milestone_id == milestone.id
        and (not scope or turn.supervisor.execution_scope in {"", scope})
        and turn.action_signal is not None
        and turn.action_signal.execution == "dispatched"
        and turn.action_signal.target != "off_target"
        and (turn.target_verify is None or turn.target_verify.on_target)
    ]
    entry_surface = next(
        (turn.action_signal.surface_id for turn in events if turn.action_signal.surface_id),
        "",
    )
    terminal = next(
        (
            turn
            for turn in reversed(events)
            if _is_terminal_boundary(turn, entry_surface)
        ),
        None,
    )
    before_terminal = [
        turn for turn in events if terminal is None or turn.index < terminal.index
    ]
    writes = [turn for turn in before_terminal if _is_write(turn)]
    latest_write = writes[-1] if writes else None
    latest_dispatch = events[-1] if events else None
    receipts = tuple(
        receipt
        for turn in writes
        if (receipt := turn.action_signal.mutation_receipt) is not None
        and receipt.statement_id == milestone.id
    )

    if terminal is not None:
        signal = terminal.action_signal
        acknowledged = bool(
            signal is not None
            and {"url", "persistence"}.intersection(signal.response_channels)
        )
        return PersistenceAssessment(
            status="submitted" if acknowledged else "pending",
            pending_receipts=() if acknowledged else receipts,
            latest_write=latest_write,
            terminal_turn=terminal,
            entry_surface=entry_surface,
            orphan_commit=not writes,
        )

    if writes:
        latest_signal = latest_dispatch.action_signal if latest_dispatch is not None else None
        return PersistenceAssessment(
            status="pending",
            pending_receipts=receipts,
            latest_write=latest_write,
            entry_surface=entry_surface,
            terminal_ready=bool(
                latest_signal is not None
                and latest_signal.role == "commit"
                and entry_surface
                and latest_signal.surface_id
                and latest_signal.surface_id != entry_surface
                and current_surface == entry_surface
            ),
        )
    return PersistenceAssessment(status="clean", entry_surface=entry_surface)


def _is_write(turn: PolicyTurn) -> bool:
    signal = turn.action_signal
    return bool(
        signal is not None
        and signal.role == "write"
        and (signal.mutation_receipt is not None or signal.target != "off_target")
    )


def _is_terminal_boundary(turn: PolicyTurn, entry_surface: str) -> bool:
    signal = turn.action_signal
    return bool(
        signal is not None
        and signal.role == "commit"
        and (
            not entry_surface
            or not signal.surface_id
            or signal.surface_id == entry_surface
        )
    )


__all__ = ["PersistenceAssessment", "assess_persistence"]
