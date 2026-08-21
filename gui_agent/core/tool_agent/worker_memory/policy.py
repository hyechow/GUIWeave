"""Deterministic event construction and memory transition validation."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from gui_agent.core.tool_agent.contracts import WorkerState

from .journal import ActionOutcome, ActionReceipt, WorkerJournalEvent
from .reducer import reduce_worker_events


_SPATIAL_ARGS = {"x", "y", "to_x", "to_y", "target_ref"}
_RESULT_FIELDS = (
    "status", "action_type", "no_effect", "kind", "ref", "requirement_id",
    "row_count", "coverage", "summary", "reason", "error", "recovery",
    "platform_feedback", "target_signal",
)


def _semantic_args(args: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in args.items() if key not in _SPATIAL_ARGS}


def _semantic_result(result: Any) -> Any:
    if not isinstance(result, dict):
        return result
    return {
        key: result[key] for key in _RESULT_FIELDS
        if key in result and result[key] not in (None, "", [], {})
    }


def _action_outcome(result: Any) -> ActionOutcome:
    if not isinstance(result, dict):
        return ActionOutcome("invoked", detail=result)
    action_type = str(result.get("action_type") or "")
    signal = result.get("target_signal")
    signal = signal if isinstance(signal, dict) else {}
    target = str(signal.get("actual_element") or "").strip()
    detail = _semantic_result(result)
    if result.get("error") or result.get("status") in {"error", "failed"}:
        kind = "failed"
    elif signal.get("status") == "off_target":
        kind = "off_target"
    elif result.get("no_effect"):
        kind = "no_effect"
    elif result.get("candidate_commit") or signal.get("status") == "on_target":
        kind = "effect"
    elif result.get("kind") == "result" or str(result.get("ref") or "").startswith("result:"):
        kind = "effect"
    else:
        kind = "invoked"
    return ActionOutcome(kind, action_type=action_type, target=target, detail=detail)


def action_receipt_event(
    *,
    worker_id: str,
    step: int,
    frame_id: str,
    tool: str,
    args: dict[str, Any],
    result: Any,
    commitment_refs: tuple[str, ...],
    substep: int | None = None,
) -> WorkerJournalEvent:
    candidate_commit = isinstance(result, dict) and bool(result.get("candidate_commit"))
    event_ref = f"step:{step}.{substep}" if substep is not None else f"step:{step}"
    receipt = ActionReceipt(
        tool=tool, args=_semantic_args(args), outcome=_action_outcome(result),
        commitment_refs=commitment_refs, preserves_window=tool == "ask_user",
        executed=isinstance(result, dict) and result.get("status") == "executed",
    )
    return WorkerJournalEvent(
        event_ref=event_ref,
        kind="candidate_commit" if candidate_commit else "action_receipt",
        frame_id=frame_id, attempt_id=worker_id, receipt=receipt,
    )


def memory_update_events(
    events: tuple[WorkerJournalEvent, ...],
    *,
    worker_id: str,
    step: int,
    frame_id: str,
    state: WorkerState,
) -> tuple[WorkerJournalEvent, ...]:
    """Return one atomic, validated memory delta without mutating the Journal."""

    pending: list[WorkerJournalEvent] = []

    def latest(fact_ref: str) -> WorkerJournalEvent | None:
        return next(
            (event for event in reversed(pending) if event.fact_ref == fact_ref),
            next(
                (
                    event for event in reversed(events)
                    if event.kind == "memory_update" and event.fact_ref == fact_ref
                ),
                None,
            ),
        )

    for index, update in enumerate(state.memory_updates, start=1):
        fact_ref = f"{update.fact_type}:{update.key}"
        prior = latest(fact_ref)
        if prior is not None and prior.origin == "runtime":
            raise ValueError(f"cannot modify Runtime-owned memory {fact_ref!r}")
        if update.status != "active" and prior is None:
            raise ValueError(f"cannot {update.status} unknown memory {fact_ref!r}")

        dependencies: list[str] = []
        if update.status == "active":
            for dependency in update.depends_on:
                event = latest(dependency)
                if event is None or event.status != "active":
                    raise ValueError(
                        f"memory {fact_ref!r} has no active dependency {dependency!r}"
                    )
                if event.lifetime == "frame" and event.frame_id != frame_id:
                    raise ValueError(
                        f"memory {fact_ref!r} depends on expired {dependency!r}"
                    )
                dependencies.append(event.event_ref)
        elif prior is not None:
            dependencies.extend(prior.depends_on)

        pending.append(WorkerJournalEvent(
            event_ref=f"step:{step}:memory:{index}",
            kind="memory_update",
            fact_type=update.fact_type,
            key=update.key,
            status=update.status,
            lifetime=update.lifetime,
            statement=" ".join(update.statement.split()),
            frame_id=frame_id,
            attempt_id=worker_id,
            origin="worker",
            depends_on=tuple(dependencies),
            supersedes=prior.event_ref if prior is not None else "",
        ))

    staged = tuple(events) + tuple(
        replace(event, sequence=len(events) + offset)
        for offset, event in enumerate(pending, start=1)
    )
    view = reduce_worker_events(staged, worker_id=worker_id, current_frame_id=frame_id)
    if state.status == "executing" and not view.active_commitments:
        raise ValueError(
            "executing phase requires an active Commitment with valid dependencies"
        )
    if state.status == "executing" and view.pending_runtime_evidence:
        raise ValueError(
            "executing phase requires authoritative runtime Evidence to be "
            "integrated through Claim and Commitment dependencies"
        )
    return tuple(pending)
