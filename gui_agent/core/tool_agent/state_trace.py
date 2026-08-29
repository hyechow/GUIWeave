"""Compact continuous State memory and Actor projection."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from gui_agent.core.tool_agent.contracts import (
    WorkerSpec,
    WorkerStateSnapshot,
    WorkerStateTarget,
    WorkerStateUpdate,
    WorkerTaskTransition,
)

from gui_agent.core.tool_agent.worker_memory.journal import WorkerJournal


def initial_worker_state() -> WorkerStateSnapshot:
    return WorkerStateSnapshot(
        summary="No goal-relevant facts have been observed yet.",
    )


def _project_receipt(event: Any) -> dict[str, Any]:
    receipt = event.receipt
    assert receipt is not None
    outcome: dict[str, Any] = {
        "kind": receipt.outcome.kind,
        "action_type": receipt.outcome.action_type or None,
    }
    if receipt.outcome.target:
        outcome["target"] = receipt.outcome.target
    projected: dict[str, Any] = {
        "receipt_ref": event.event_ref,
        "tool": receipt.tool,
        "executed": receipt.executed,
        "outcome": outcome,
    }
    if receipt.target_ref:
        projected["target_ref"] = receipt.target_ref
    if receipt.state_target_ref:
        projected["state_target_ref"] = receipt.state_target_ref
    if description := str(receipt.args.get("description") or "").strip():
        projected["action_description"] = description
    if receipt.tool in {"scroll", "drag"} and receipt.args.get("direction"):
        projected["traversal_direction"] = receipt.args["direction"]
    return projected


def latest_runtime_receipts(journal: WorkerJournal) -> list[dict[str, Any]]:
    """Project only the most recent atomic or multi-action receipt batch."""

    events = [event for event in journal.events if event.receipt is not None]
    if not events:
        return []
    latest_step = events[-1].event_ref.split(":", 1)[-1].split(".", 1)[0]
    batch = [
        event for event in events
        if event.event_ref.split(":", 1)[-1].split(".", 1)[0] == latest_step
    ]
    return [_project_receipt(event) for event in batch[-5:]]


def latest_runtime_receipt(journal: WorkerJournal) -> dict[str, Any] | None:
    """Project the latest receipt for call sites that need one action."""

    receipts = latest_runtime_receipts(journal)
    return receipts[-1] if receipts else None


def state_target_ref(identity: str) -> str:
    """Derive one stable opaque binding from a model-observed identity."""

    digest = hashlib.sha256(identity.strip().casefold().encode()).hexdigest()[:16]
    return f"target_{digest}"


def _summary(state: WorkerStateSnapshot) -> str:
    return (
        f"Current targets={len(state.targets)}; "
        f"durable facts={len(state.memory)}."
    )


def reduce_worker_state(
    previous: WorkerStateSnapshot | None,
    update: WorkerStateUpdate,
    *,
    frame_id: str,
) -> WorkerStateSnapshot:
    """Merge one compact fact patch and derive Runtime-owned target refs."""

    state = initial_worker_state() if previous is None else previous.model_copy(deep=True)
    state.frame_id = frame_id
    for key, value in update.memory.items():
        if value is None:
            state.memory.pop(key, None)
        else:
            state.memory[key] = value
    if len(json.dumps(state.memory, ensure_ascii=False)) > 16_000:
        raise ValueError("State memory exceeds 16000 characters")

    state.targets = {
        state_target_ref(identity): WorkerStateTarget(
            identity=identity,
            visibility="full",
            owned_region_visibility="unobscured",
        )
        for identity in update.targets
    }
    state.task_transition = WorkerTaskTransition(
        status=update.status,
        next_objective=update.objective,
        target_refs=list(state.targets),
    )
    state.summary = _summary(state)
    return state


def state_continuation_payload(state: WorkerStateSnapshot) -> dict[str, Any]:
    """Return only durable facts and the prior semantic conclusion."""

    transition = state.task_transition
    return {
        "memory": state.memory,
        "previous_transition": (
            {
                "status": transition.status,
                "objective": transition.next_objective,
                "targets": [
                    state.targets[ref].identity
                    for ref in transition.target_refs
                    if ref in state.targets
                ],
            }
            if transition is not None else None
        ),
    }


def state_actor_markdown(state: WorkerStateSnapshot) -> str:
    """Render the compact State conclusion for the action-only Actor."""

    transition = state.task_transition
    lines = [
        "## Current task objective",
        "",
        *(
            [
                f"Status: `{transition.status}`",
                f"Next objective: {transition.next_objective}",
                "Authorized target refs: "
                + (
                    ", ".join(f"`{ref}`" for ref in transition.target_refs)
                    or "None (use only untracked interface controls)"
                ),
            ]
            if transition is not None
            else ["No State transition has been concluded yet."]
        ),
        "",
        "## Authorized visible targets",
        "",
    ]
    if not state.targets:
        lines.append("- None")
    else:
        lines.extend(
            f"- `{ref}` — {target.identity}"
            for ref, target in state.targets.items()
        )
    lines.extend([
        "",
        "## Continuous factual memory",
        "",
        json.dumps(state.memory, ensure_ascii=False, sort_keys=True)
        if state.memory else "{}",
    ])
    return "\n".join(lines)


def state_observation_focus(spec: WorkerSpec) -> dict[str, Any]:
    """Expose fact shapes and the full goal contract to State."""

    visible_fields = sorted({
        str(value)
        for requirement in spec.data_requirements
        for value in (
            *requirement.field_sources.values(),
            *(requirement.row_schema.get("properties") or {}).keys(),
        )
        if str(value).strip()
    })
    return {
        "visible_fields": visible_fields,
        "goal_contract": {
            "goal": spec.goal,
            "success_criteria": list(spec.success_criteria),
            "completion_facts": [
                {
                    "property_ref": item.property_ref,
                    "description": item.description,
                    "expected_value": item.expected_value,
                }
                for item in spec.completion_facts
            ],
        },
    }
