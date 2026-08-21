"""Pure temporal reduction of Worker journal events."""

from __future__ import annotations

from dataclasses import dataclass

from .journal import WorkerJournal, WorkerJournalEvent


DEFAULT_WORKER_RECENT_K = 4
_RECENT_TRANSITION_LIMIT = 8


@dataclass(frozen=True)
class WorkerMemoryView:
    worker_id: str
    current_observations: tuple[WorkerJournalEvent, ...] = ()
    accumulated_evidence: tuple[WorkerJournalEvent, ...] = ()
    established_claims: tuple[WorkerJournalEvent, ...] = ()
    active_commitments: tuple[WorkerJournalEvent, ...] = ()
    transition_commitments: tuple[WorkerJournalEvent, ...] = ()
    pending_runtime_evidence: tuple[WorkerJournalEvent, ...] = ()
    latest_gui_transition: tuple[WorkerJournalEvent, ...] = ()
    recent_receipts: tuple[WorkerJournalEvent, ...] = ()
    state_timeline: tuple[str, ...] = ()

    def render_prompt_section(self) -> str:
        from .renderer import render_worker_memory

        return render_worker_memory(self)


def reduce_worker_events(
    events: tuple[WorkerJournalEvent, ...],
    *,
    worker_id: str,
    current_frame_id: str = "",
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
    valid_by_ref: dict[str, WorkerJournalEvent] = {}
    for event in sorted(candidates, key=lambda item: item.sequence):
        if all(dependency in valid_by_ref for dependency in event.depends_on):
            valid_by_ref[event.event_ref] = event
    valid = tuple(valid_by_ref.values())
    observations = tuple(event for event in valid if event.fact_type == "observation")
    evidence = tuple(event for event in valid if event.fact_type == "evidence")
    claims = tuple(event for event in valid if event.fact_type == "claim")
    commitments = tuple(event for event in valid if event.fact_type == "commitment")

    event_by_ref = {event.event_ref: event for event in memory_events}
    completed = tuple(
        event for event in memory_events
        if event.fact_type == "commitment" and event.status == "completed"
    )
    integrated: set[str] = set()
    pending_dependencies = [
        dependency
        for commitment in (*commitments, *completed)
        for dependency in commitment.depends_on
    ]
    while pending_dependencies:
        dependency = pending_dependencies.pop()
        if dependency in integrated:
            continue
        integrated.add(dependency)
        parent = event_by_ref.get(dependency)
        if parent is not None:
            pending_dependencies.extend(parent.depends_on)
    pending_runtime = tuple(
        event for event in evidence
        if event.requires_integration and event.event_ref not in integrated
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
    bound_refs = {
        ref for event in latest_transition if event.receipt
        for ref in event.receipt.commitment_refs
    }
    transition_commitments = tuple(
        event for event in memory_events
        if event.event_ref in bound_refs and event.fact_type == "commitment"
    )

    def current_validity(event: WorkerJournalEvent) -> str:
        if latest.get(event.fact_ref) is not event:
            return "superseded"
        if event.status != "active":
            return str(event.status)
        if event.event_ref in valid_by_ref:
            return "active"
        if event.lifetime == "frame":
            return "expired"
        return "invalidated"

    timeline = tuple(
        f"t={event.sequence}: {event.fact_ref}; event_status={event.status}; "
        f"now={current_validity(event)}; lifetime={event.lifetime}"
        for event in memory_events[-_RECENT_TRANSITION_LIMIT:]
    )
    return WorkerMemoryView(
        worker_id=worker_id,
        current_observations=observations,
        accumulated_evidence=evidence,
        established_claims=claims,
        active_commitments=commitments,
        transition_commitments=transition_commitments,
        pending_runtime_evidence=pending_runtime,
        latest_gui_transition=latest_transition,
        recent_receipts=recent,
        state_timeline=timeline,
    )


def build_worker_memory_view(
    journal: WorkerJournal,
    *,
    current_frame_id: str = "",
    recent_k: int = DEFAULT_WORKER_RECENT_K,
) -> WorkerMemoryView:
    return reduce_worker_events(
        tuple(journal.events),
        worker_id=journal.worker_id,
        current_frame_id=current_frame_id,
        recent_k=recent_k,
    )
