"""Pure append-only reduction for goal-oriented Worker State."""

from __future__ import annotations

import re
from typing import Any

from gui_agent.core.tool_agent.contracts import (
    WorkerCoverageObserved,
    WorkerGoalConditionObserved,
    WorkerPropertyObserved,
    WorkerSourceObserved,
    WorkerSpec,
    WorkerStateCoverage,
    WorkerStateGoalCondition,
    WorkerStateProperty,
    WorkerStateSnapshot,
    WorkerStateTarget,
    WorkerStateTraceBatch,
    WorkerSurfaceObserved,
    WorkerTargetObserved,
)
from gui_agent.core.tool_agent.worker_memory.journal import WorkerJournal


STATE_TRACE_OUTPUT_CONTRACT: dict[str, Any] = {
    "root": {"mode": "init|append", "frame_id": "copy input", "delta": "object"},
    "delta_encoding": "Omit empty groups. Singleton groups are one tuple; plural groups are tuple lists.",
    "delta": {
        "source": ["source_ref", "evidence"],
        "surface": ["surface", "evidence"],
        "targets": [[
            "target_ref", "stable concise identity", "partial|full",
            "edge_fragment|unobscured", "evidence",
        ]],
        "properties": [[
            "target_ref", "property_ref", "json scalar", "unresolved|resolved",
            "ambiguous_visual|bound_visual|explicit_visual", "evidence",
        ]],
        "coverage": [["source_ref", "unresolved|exhausted", "evidence"]],
        "conditions": [["criterion_N", "unresolved|satisfied|blocked", "evidence"]],
    },
    "example": {"properties": [[
        "target_ref", "requested_state", True, "resolved", "explicit_visual",
        "target-owned control confirms the value",
    ]]},
    "ref_format": "lower_snake_case; property_ref may contain dots",
}

_STATE_DELTA_FIELDS: dict[str, tuple[str, tuple[str, ...], bool]] = {
    "source": ("source_observed", ("source_ref", "evidence"), True),
    "surface": ("surface_observed", ("surface", "evidence"), True),
    "targets": ("target_observed", (
        "target_ref", "identity", "visibility", "owned_region_visibility", "evidence",
    ), False),
    "properties": ("property_observed", (
        "target_ref", "property_ref", "value", "goal_relation", "authority", "evidence",
    ), False),
    "coverage": ("coverage_observed", ("source_ref", "status", "evidence"), False),
    "conditions": ("goal_condition_observed", (
        "condition_ref", "status", "evidence",
    ), False),
}


def normalize_state_trace_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Expand the compact grouped delta and normalize its stable refs."""

    normalized = dict(payload)
    raw_delta = normalized.get("delta")
    if not isinstance(raw_delta, dict):
        return normalized
    raw_events: list[dict[str, Any]] = []
    for group, value in raw_delta.items():
        spec = _STATE_DELTA_FIELDS.get(group)
        if spec is None:
            return normalized
        kind, fields, singleton = spec
        rows = [value] if singleton else value
        if not isinstance(rows, list):
            return normalized
        for row in rows:
            if not isinstance(row, list) or len(row) != len(fields):
                return normalized
            raw_events.append({"kind": kind, **dict(zip(fields, row, strict=True))})

    def stable_ref(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        ref = re.sub(r"[^a-z0-9_]+", "_", value.casefold()).strip("_")
        ref = re.sub(r"_+", "_", ref)
        return (ref if ref[:1].isalpha() else f"ref_{ref}")[:80].rstrip("_")

    events: list[dict[str, Any]] = []
    for raw_event in raw_events:
        event = dict(raw_event)
        for field in ("source_ref", "target_ref"):
            if field in event:
                event[field] = stable_ref(event[field])
        events.append(event)
    normalized.pop("delta")
    normalized["events"] = events
    return normalized


def initial_worker_state(spec: WorkerSpec) -> WorkerStateSnapshot:
    conditions = {
        f"criterion_{index}": WorkerStateGoalCondition(statement=statement)
        for index, statement in enumerate(spec.success_criteria, start=1)
    }
    return WorkerStateSnapshot(
        status="exploring",
        summary="The goal source and current difference have not been established yet.",
        goal_conditions=conditions,
    )


def latest_runtime_receipt(journal: WorkerJournal) -> dict[str, Any] | None:
    """Project the latest receipt as observation context, never as State."""

    event = next((item for item in reversed(journal.events) if item.receipt), None)
    if event is None or event.receipt is None:
        return None
    receipt = event.receipt
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
    if receipt.tool in {"scroll", "drag"} and receipt.args.get("direction"):
        projected["traversal_direction"] = receipt.args["direction"]
    return projected


def _target_relation(target: WorkerStateTarget) -> str:
    relations = {item.goal_relation for item in target.properties.values()}
    if relations and relations == {"resolved"}:
        return "resolved"
    return "unresolved"


def _status(view: WorkerStateSnapshot) -> str:
    conditions = tuple(view.goal_conditions.values())
    if any(item.status == "blocked" for item in conditions):
        return "failed"
    if conditions and all(item.status == "satisfied" for item in conditions):
        return "completed"
    if view.source_ref is None:
        return "exploring"
    if any(item.status == "unresolved" for item in view.coverage.values()):
        return "collecting"
    return "executing"


def _summary(view: WorkerStateSnapshot) -> str:
    conditions = tuple(view.goal_conditions.values())
    satisfied = sum(item.status == "satisfied" for item in conditions)
    blocked = sum(item.status == "blocked" for item in conditions)
    coverage = ", ".join(
        f"{ref}={item.status}" for ref, item in view.coverage.items()
    ) or "not tracked"
    return (
        f"Source={view.source_ref or 'unresolved'}; surface={view.surface or 'unknown'}; "
        f"goal conditions satisfied={satisfied}/{len(conditions)}, blocked={blocked}; "
        f"coverage={coverage}; tracked targets={len(view.targets)}."
    )


def reduce_worker_state(
    previous: WorkerStateSnapshot | None,
    batch: WorkerStateTraceBatch,
    *,
    spec: WorkerSpec,
) -> WorkerStateSnapshot:
    """Append one frame's typed observations to continuous target memory."""

    expected_mode = "init" if previous is None else "append"
    if batch.mode != expected_mode:
        raise ValueError(f"expected State mode {expected_mode!r}, got {batch.mode!r}")
    view = initial_worker_state(spec) if previous is None else previous.model_copy(deep=True)
    view.frame_id = batch.frame_id
    for target in view.targets.values():
        target.visibility = "not_visible"
        target.owned_region_visibility = "not_visible"

    for event in batch.events:
        if isinstance(event, WorkerSourceObserved):
            if event.source_ref != view.source_ref:
                view.source_ref = event.source_ref
        elif isinstance(event, WorkerSurfaceObserved):
            if event.surface != view.surface:
                view.surface = event.surface
        elif isinstance(event, WorkerTargetObserved):
            if view.source_ref is None:
                continue
            target = view.targets.get(event.target_ref)
            if target is None:
                target = WorkerStateTarget(
                    identity=event.identity,
                    source_ref=view.source_ref,
                )
                view.targets[event.target_ref] = target
            target.identity = event.identity
            target.source_ref = view.source_ref
            target.visibility = event.visibility
            target.owned_region_visibility = event.owned_region_visibility
        elif isinstance(event, WorkerPropertyObserved):
            target = view.targets.get(event.target_ref)
            if target is None:
                continue
            prior = target.properties.get(event.property_ref)
            if prior is not None and prior.goal_relation == "resolved" and (
                event.goal_relation != "resolved"
            ):
                continue
            if prior is not None and (
                prior.value,
                prior.goal_relation,
                prior.authority,
            ) == (
                event.value,
                event.goal_relation,
                event.authority,
            ):
                continue
            target.properties[event.property_ref] = WorkerStateProperty(
                value=event.value,
                goal_relation=event.goal_relation,
                authority=event.authority,
                frame_id=batch.frame_id,
                evidence=event.evidence,
            )
        elif isinstance(event, WorkerCoverageObserved):
            if event.source_ref != view.source_ref:
                continue
            prior = view.coverage.get(event.source_ref)
            if prior is not None and prior.status == event.status:
                continue
            view.coverage[event.source_ref] = WorkerStateCoverage(
                status=event.status,
                frame_id=batch.frame_id,
                evidence=event.evidence,
            )
        elif isinstance(event, WorkerGoalConditionObserved):
            condition = view.goal_conditions.get(event.condition_ref)
            if (
                condition is None
                or condition.status == "satisfied"
                or condition.status == event.status
            ):
                continue
            condition.status = event.status
            condition.frame_id = batch.frame_id
            condition.evidence = event.evidence

    view.status = _status(view)
    view.summary = _summary(view)
    return view


def state_continuation_payload(state: WorkerStateSnapshot) -> dict[str, Any]:
    """Project the small durable snapshot needed by the next append call."""

    return {
        "source_ref": state.source_ref,
        "surface": state.surface,
        "targets": {
            ref: {
                "identity": target.identity,
                "relation": _target_relation(target),
                "properties": {
                    name: {"value": item.value, "relation": item.goal_relation}
                    for name, item in target.properties.items()
                },
            }
            for ref, target in state.targets.items()
        },
        "coverage": {ref: item.status for ref, item in state.coverage.items()},
        "goal_conditions": {
            ref: item.status for ref, item in state.goal_conditions.items()
        },
    }


def state_actor_payload(state: WorkerStateSnapshot) -> dict[str, Any]:
    """Project continuous memory into the Actor's current target frontier."""

    unresolved: list[dict[str, str]] = []
    resolved_visible: list[str] = []
    for target_ref, target in state.targets.items():
        if target.visibility == "not_visible":
            continue
        if _target_relation(target) == "resolved":
            resolved_visible.append(target_ref)
        else:
            unresolved.append({
                "target_ref": target_ref,
                "visibility": target.visibility,
                "owned_region_visibility": target.owned_region_visibility,
            })
    if unresolved:
        difference = "Advance one visible unresolved target: " + ", ".join(
            item["target_ref"] for item in unresolved
        )
    elif resolved_visible and any(
        item.status == "unresolved" for item in state.coverage.values()
    ):
        difference = "Visible targets are resolved; continue source traversal."
    elif resolved_visible:
        difference = "Visible targets are resolved; do not repeat their completed predicates."
    else:
        difference = "No classified visible target establishes the next difference."
    return {
        "status": state.status,
        "source_ref": state.source_ref,
        "surface": state.surface,
        "unresolved_target_memory": {
            ref: {
                "identity": target.identity,
                "properties": {
                    name: {"value": item.value, "goal_relation": item.goal_relation}
                    for name, item in target.properties.items()
                },
            }
            for ref, target in state.targets.items()
            if _target_relation(target) != "resolved"
        },
        "resolved_target_refs": [
            ref for ref, target in state.targets.items()
            if _target_relation(target) == "resolved"
        ],
        "coverage": {ref: item.status for ref, item in state.coverage.items()},
        "goal_conditions": {
            ref: item.status for ref, item in state.goal_conditions.items()
        },
        "visible_targets": {
            "unresolved_frontier": unresolved,
            "resolved_refs_do_not_repeat": resolved_visible,
        },
        "goal_difference": difference,
    }
