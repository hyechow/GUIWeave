"""Pure temporal reduction of Worker journal events."""

from __future__ import annotations

from dataclasses import dataclass

from .journal import TargetRef, WorkerJournal, WorkerJournalEvent


DEFAULT_WORKER_RECENT_K = 4


@dataclass(frozen=True)
class TargetTransactionView:
    target: TargetRef
    status: str
    evidence: tuple[WorkerJournalEvent, ...] = ()
    receipts: tuple[WorkerJournalEvent, ...] = ()


@dataclass(frozen=True)
class WorkerMemoryView:
    worker_id: str
    current_observations: tuple[WorkerJournalEvent, ...] = ()
    accumulated_evidence: tuple[WorkerJournalEvent, ...] = ()
    active_commitments: tuple[WorkerJournalEvent, ...] = ()
    completed_commitments: tuple[WorkerJournalEvent, ...] = ()
    blocked_commitments: tuple[WorkerJournalEvent, ...] = ()
    latest_gui_transition: tuple[WorkerJournalEvent, ...] = ()
    recent_receipts: tuple[WorkerJournalEvent, ...] = ()
    target_transaction: TargetTransactionView | None = None


def reduce_worker_events(
    events: tuple[WorkerJournalEvent, ...],
    *,
    worker_id: str,
    current_frame_id: str = "",
    current_surface_fingerprint: str = "",
    recent_k: int = DEFAULT_WORKER_RECENT_K,
) -> WorkerMemoryView:
    """Reduce the same event sequence to the same view, independent of the model."""

    memory_events = tuple(
        event for event in events if event.kind == "memory_update" and event.fact_ref
    )
    if not current_frame_id:
        current_frame_id = next(
            (event.frame_id for event in reversed(memory_events) if event.frame_id),
            "",
        )

    latest: dict[str, WorkerJournalEvent] = {}
    for event in memory_events:
        latest[event.fact_ref] = event

    receipt_events = tuple(sorted(
        {
            event.event_ref: event for event in events
            if event.receipt is not None or event.feedback
        }.values(),
        key=lambda event: event.sequence,
    ))
    action_events = tuple(
        event for event in receipt_events if event.receipt is not None
    )

    def observation_is_current(event: WorkerJournalEvent) -> bool:
        if event.frame_id == current_frame_id:
            return True
        later = tuple(item for item in action_events if item.sequence > event.sequence)
        return bool(later) and all(
            item.receipt is not None and item.receipt.preserves_window for item in later
        )

    candidates = tuple(
        event for event in latest.values()
        if event.status == "active"
        and (event.lifetime != "frame" or observation_is_current(event))
    )
    valid = tuple(sorted(candidates, key=lambda item: item.sequence))
    observations = tuple(event for event in valid if event.fact_type == "observation")
    evidence = tuple(event for event in valid if event.fact_type == "evidence")
    runtime_commitments = tuple(
        event for event in latest.values()
        if event.fact_type == "commitment" and event.origin == "runtime"
    )
    commitments = tuple(
        event for event in runtime_commitments
        if event.status == "dispatched"
    )

    completed = tuple(
        event for event in runtime_commitments if event.status == "satisfied"
    )
    blocked = tuple(
        event for event in runtime_commitments if event.status in {"uncertain", "failed"}
    )

    recent_limit = max(0, int(recent_k))
    recent = receipt_events[-recent_limit:] if recent_limit else ()

    transition_anchor = action_events[-1] if action_events else None
    if not (
        transition_anchor
        and transition_anchor.receipt is not None
        and current_frame_id
        and transition_anchor.frame_id
        and transition_anchor.frame_id != current_frame_id
        and not transition_anchor.receipt.preserves_window
    ):
        transition_anchor = None
    latest_transition = tuple(
        event for event in action_events
        if transition_anchor is not None
        and event.frame_id == transition_anchor.frame_id
        and event.receipt is not None
        and not event.receipt.preserves_window
    )
    active_target: TargetRef | None = None
    for event in action_events:
        receipt = event.receipt
        if receipt is None:
            continue
        if receipt.target is not None:
            active_target = receipt.target
        if receipt.clears_target:
            active_target = None
    target_transaction = None
    if active_target is not None:
        target_transaction = TargetTransactionView(
            target=active_target,
            status=(
                "returned_to_anchor"
                if current_surface_fingerprint
                and current_surface_fingerprint == active_target.anchor_surface_fingerprint
                else "active"
            ),
            evidence=tuple(
                event for event in valid
                if event.fact_type == "evidence" and event.target_ref == active_target.ref
            ),
            receipts=tuple(
                event for event in action_events
                if event.receipt is not None
                and event.receipt.target_ref == active_target.ref
            ),
        )
    return WorkerMemoryView(
        worker_id=worker_id,
        current_observations=observations,
        accumulated_evidence=evidence,
        active_commitments=commitments,
        completed_commitments=completed,
        blocked_commitments=blocked,
        latest_gui_transition=latest_transition,
        recent_receipts=recent,
        target_transaction=target_transaction,
    )


def build_worker_memory_view(
    journal: WorkerJournal,
    *,
    current_frame_id: str = "",
    current_surface_fingerprint: str = "",
    recent_k: int = DEFAULT_WORKER_RECENT_K,
) -> WorkerMemoryView:
    return reduce_worker_events(
        tuple(journal.events),
        worker_id=journal.worker_id,
        current_frame_id=current_frame_id,
        current_surface_fingerprint=current_surface_fingerprint,
        recent_k=recent_k,
    )
