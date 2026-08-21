"""Prompt and context rendering from already-reduced Worker state."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from gui_agent.context.blocks import ContextBlock, ContextCompressor, render_context_blocks
from gui_agent.core.tool_agent.contracts import MaterializedFrame

from .journal import WorkerJournalEvent
from .reducer import WorkerMemoryView


DEFAULT_WORKER_CONTEXT_MAX_CHARS = int(
    os.environ.get("TOOL_AGENT_WORKER_CONTEXT_MAX_CHARS") or 24_000
)

_CONTROL_FIELDS = (
    "kind", "label", "placeholder", "value", "selected", "selection_mode",
    "selected_text", "selected_text_primary", "options", "focused", "enabled",
    "required", "is_filter", "query_action", "form_action", "is_datepicker",
    "group_index", "group_field", "row_values", "in_viewport", "viewport_pos",
    "occluded", "rect",
)
_CHOICE_FIELDS = ("selected_text", "selected_text_primary")
_SIGNIFICANT_EMPTY_FIELDS = ("value", *_CHOICE_FIELDS)


def _bounded_json(value: Any, *, limit: int = 1_200) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render_application_knowledge_context(application_knowledge: str) -> str:
    knowledge = application_knowledge.strip()
    if not knowledge:
        return ""
    return (
        "## Application knowledge (session-scoped execution facts)\n"
        "These facts define application mechanics and non-visual postconditions, not task "
        "requirements. Reconcile them with ordered receipts; a current frame cannot negate "
        "a matching non-visual postcondition.\n"
        + knowledge
    )


def _receipt_text(event: WorkerJournalEvent) -> str:
    if event.feedback:
        return event.feedback
    receipt = event.receipt
    if receipt is None:
        return ""
    outcome = receipt.outcome
    args = receipt.args
    intent = str(args.get("description") or "").strip()
    if event.kind == "candidate_commit":
        text = "candidate selection commit produced a confirmed transition"
    elif outcome.kind == "failed":
        text = f"tool={receipt.tool}; failure={_bounded_json(outcome.detail, limit=420)}"
    elif outcome.kind == "off_target":
        actual = f"; marker landed on {outcome.target!r}" if outcome.target else ""
        action = f"; action={outcome.action_type}" if outcome.action_type else ""
        intent_text = f"; intent={intent!r}" if intent else ""
        text = (
            f"tool={receipt.tool}; flash verifier reported off_target{actual}"
            f"{action}{intent_text}; do not repeat the same point"
        )
    elif outcome.kind == "no_effect":
        target = f"; target={outcome.target!r}" if outcome.target else ""
        if outcome.action_type in {"type", "select_option", "clear_text"}:
            instruction = "verify the current control value before any retry"
        else:
            instruction = (
                "reconcile application mechanics and current evidence; an unchanged "
                "screen alone does not justify repeating the action"
            )
        text = (
            f"tool={receipt.tool}; action={outcome.action_type}{target}; "
            f"invocation=confirmed; screen_transition=none_observed; {instruction}"
        )
    elif outcome.kind == "effect" and outcome.target:
        text = (
            f"tool={receipt.tool}; action={outcome.action_type}; target={outcome.target!r}; "
            f"intent={intent!r}; status=executed"
        )
    elif outcome.kind == "effect":
        text = f"tool={receipt.tool}; result={_bounded_json(outcome.detail, limit=420)}"
    else:
        args_text = f"; args={_bounded_json(args, limit=220)}" if args else ""
        text = (
            f"tool={receipt.tool}{args_text}; "
            f"result={_bounded_json(outcome.detail, limit=260)}"
        )
    if outcome.kind == "no_effect" and args:
        text += f"; args={_bounded_json(args, limit=220)}"
    return text


def _phase_guidance(memory: WorkerMemoryView) -> str:
    if memory.pending_runtime_evidence:
        return "Integrate each authoritative Evidence into a dependent Claim and Commitment before executing."
    if memory.latest_gui_transition and memory.transition_commitments:
        return (
            "Reconcile only the Commitments bound to the latest invocation. Complete an "
            "exact bound key when satisfied; otherwise keep it active. If application "
            "knowledge identifies the invocation as write-through and this is its normal "
            "error-free post-commit frame, complete instead of repeating the mutation. "
            "Never rename or repeat it."
        )
    if memory.latest_gui_transition:
        return (
            "No Commitment was bound to the latest invocation. Reconcile its receipts with "
            "the frame and record Evidence, but do not complete or invent a Commitment."
        )
    if memory.active_commitments:
        return "Execute the active Commitment from exact dependencies; reacquire only if one is invalidated."
    if memory.established_claims:
        return "Preserve established Claims and acquire only unresolved Evidence."
    return "Continue the current attempt from verified current-frame evidence."


def render_worker_memory(memory: WorkerMemoryView) -> str:
    projected = (
        *memory.current_observations,
        *memory.accumulated_evidence,
        *memory.established_claims,
        *memory.active_commitments,
    )
    fact_ref_by_event = {event.event_ref: event.fact_ref for event in projected}

    def dependencies(event: WorkerJournalEvent) -> tuple[str, ...]:
        return tuple(
            fact_ref_by_event.get(dependency, dependency)
            for dependency in event.depends_on
        )

    lines = [
        "## WorkerMemory (runtime-projected; not conversation history)",
        "This is a deterministic projection of an append-only event journal. A newer "
        "frame does not overwrite evidence with a different key; only an explicit "
        "update, expiry, or invalid dependency changes active memory. Evidence records "
        "what held at its source time; only the current frame proves present visibility "
        "or actionability.",
    ]
    sections = (
        ("Current-frame observations", memory.current_observations),
        ("Accumulated evidence", memory.accumulated_evidence),
        ("Established claims", memory.established_claims),
        ("Active commitments", memory.active_commitments),
    )
    for heading, events in sections:
        lines.append(f"### {heading}")
        lines.extend(
            f"- [t={event.sequence}; {event.fact_ref}; lifetime={event.lifetime}; "
            f"status={event.status}; source={event.frame_id or event.origin}] "
            f"{event.statement}"
            + (
                f"; depends_on={json.dumps(dependencies(event), ensure_ascii=False)}"
                if event.depends_on else ""
            )
            for event in events
        )
        if not events:
            lines.append("- None.")

    if memory.transition_commitments:
        lines.append("### Commitments bound to latest invocation")
        lines.extend(
            f"- [t={event.sequence}; {event.fact_ref}; source={event.frame_id}] "
            f"{event.statement}"
            for event in memory.transition_commitments
        )
    if memory.latest_gui_transition:
        lines.extend([
            "### Latest GUI transition (immediately before the current frame)",
            "- Reconcile these ordered invocation receipts as one transaction with the "
            "current frame before retrying. Each receipt proves its action was invoked; "
            "the current frame shows the transaction's effect. 'Visual effect unconfirmed' "
            "is not an execution failure.",
        ])
        lines.extend(
            f"- [t={event.sequence}; {event.event_ref}] {_receipt_text(event)}"
            for event in memory.latest_gui_transition
        )

    earlier = tuple(
        event for event in memory.recent_receipts
        if event not in memory.latest_gui_transition
    )
    if earlier:
        lines.append("### Earlier execution receipts")
        lines.extend(
            f"- [t={event.sequence}; {event.event_ref}] {_receipt_text(event)}"
            for event in earlier
        )
    if memory.pending_runtime_evidence:
        lines.append("### Authoritative evidence awaiting integration")
        lines.extend(
            f"- {event.fact_ref}: {event.statement}"
            for event in memory.pending_runtime_evidence
        )

    lines.extend(["### Permitted next phase", f"- {_phase_guidance(memory)}"])

    lines.append("### Memory update history (event-time status; current validity is above)")
    lines.extend(f"- {item}" for item in memory.state_timeline)
    if not memory.state_timeline:
        lines.append("- No memory transitions recorded.")
    if not any((projected, memory.recent_receipts, memory.state_timeline)):
        lines.extend(["### History", "- No prior Worker actions."])
    return "\n".join(lines)


def _info_coverage(coverage: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in coverage.items() if key != "status"}


def _semantic_controls(controls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            key: control[key]
            for key in _CONTROL_FIELDS
            if key in control
            and (key in _SIGNIFICANT_EMPTY_FIELDS or control[key] not in (None, "", [], {}))
        }
        for control in controls
        if isinstance(control, dict) and control.get("in_viewport") is not False
    ]


def _observed_choice_state(controls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            key: control[key]
            for key in ("kind", "label", *_SIGNIFICANT_EMPTY_FIELDS)
            if key in control
        }
        for control in controls
        if isinstance(control, dict)
        and control.get("in_viewport") is False
        and any(key in control for key in _CHOICE_FIELDS)
    ]


def _collection_ref(item: Any) -> dict[str, Any]:
    result = {
        "ref": item.ref, "requirement_id": item.requirement_id,
        "row_count": item.row_count, "coverage": _info_coverage(item.coverage),
    }
    if getattr(item, "provider", ""):
        result["provider"] = item.provider
    return result


def _frame_payload(frame: MaterializedFrame, *, candidate_committed: bool) -> dict[str, Any]:
    scope_blockers: dict[str, dict[str, Any]] = {}
    for requirement_id, scope in frame.requirement_scopes.items():
        if str(scope.get("status") or "") != "unmet":
            continue
        detail = scope.get("detail_resolution")
        if (
            isinstance(detail, dict)
            and detail.get("status") == "active"
            and detail.get("pending_candidate_ordinal") is not None
        ):
            continue
        requested = dict(scope.get("requested_filters") or {})
        applied = dict(scope.get("applied_filters") or {})
        scope_blockers[requirement_id] = {
            "status": "unmet",
            "missing_applied_filters": sorted(set(requested).difference(applied)),
            "extra_applied_filters": sorted(set(applied).difference(requested)),
            "conflicting_applied_filters": sorted(
                key for key in set(requested).intersection(applied)
                if requested[key] != applied[key]
            ),
        }
    candidate_state: dict[str, Any] = {}
    if candidate_committed:
        filters = [
            item for item in frame.controls
            if item.get("in_viewport") is not False
            and str(item.get("kind") or "").casefold() in {"text_input", "textbox"}
            and item.get("is_filter") is True
        ]
        if len(filters) == 1 and not any(
            item.get("in_viewport") is not False
            and item.get("selection_mode") == "multiple"
            for item in frame.controls
        ):
            anchor = filters[0]
            value = str(anchor.get("value") or "").strip().casefold()
            if value in {"", str(anchor.get("label") or "").casefold()}:
                candidate_state = {"status": "exhausted"}
    return {
        "frame_id": frame.frame_id,
        "readiness": frame.readiness,
        "readiness_reason": frame.readiness_reason,
        "task_reference_time": frame.platform_time,
        "url": frame.url,
        "title": frame.title,
        "applied_filters": frame.applied_filters,
        "requirement_scopes": frame.requirement_scopes,
        "scope_blockers": scope_blockers,
        "observed_choice_state": _observed_choice_state(frame.controls),
        "candidate_set_state": candidate_state,
        "visible_collection_regions": frame.visible_collection_regions,
        "structured_surfaces": frame.structured_surfaces,
        "chunks": [_collection_ref(item) for item in frame.chunks],
        "collections": [_collection_ref(item) for item in frame.collections],
        "missing_requirements": frame.missing_requirements,
        **({"controls": _semantic_controls(frame.controls)} if frame.controls else {}),
    }


@dataclass(frozen=True)
class WorkerContextProjection:
    text: str
    report: dict[str, Any]


def project_worker_context(
    *,
    memory: WorkerMemoryView,
    frame: MaterializedFrame,
    application_knowledge: str = "",
    attempt_contract: str = "",
    same_frame_feedback: dict[str, Any] | None = None,
    max_chars: int = DEFAULT_WORKER_CONTEXT_MAX_CHARS,
) -> WorkerContextProjection:
    memory_text = render_worker_memory(memory)
    if len(memory_text) > max_chars:
        raise MemoryError("memory_overflow: active WorkerMemory exceeds the context budget")
    compact_frame = json.dumps(_frame_payload(
        frame,
        candidate_committed=any(
            event.kind == "candidate_commit" for event in memory.recent_receipts
        ),
    ), ensure_ascii=False)
    def block(
        block_id: str, source_type: str, source: str, ttl: str, priority: int,
        content: str, **metadata: Any,
    ) -> ContextBlock:
        return ContextBlock(
            id=f"tool_agent.worker.{block_id}", source_type=source_type,
            source=source, ttl=ttl, budget="required", priority=priority,
            content=content, **metadata,
        )

    blocks = [
        block(
            "same_frame_feedback", "runtime_feedback", "runtime_feedback", "turn", 5,
            "## Same-frame runtime feedback\n"
            + json.dumps(same_frame_feedback, ensure_ascii=False, default=str),
        ) if same_frame_feedback else None,
        block(
            "current_attempt", "runtime_contract", "worker_spec", "turn", 5,
            attempt_contract, authoritative_for=("goal", "output_contract", "approach"),
            freshness="turn", coverage="complete",
        ) if attempt_contract.strip() else None,
        block(
            "current_frame", "runtime_observation", "materialized_frame", "turn", 20,
            "## Current MaterializedFrame (compact semantic projection)\n"
            "Runtime-owned collection/data values remain private. Visible collection cell "
            "text is exact current-frame evidence, but cells do not declare record "
            "boundaries; clipped cells may be incomplete. For every control rect, x/y are "
            "its normalized center coordinates; never add half of w/h to derive the action "
            "point.\n" + compact_frame,
            freshness="turn", coverage="complete",
        ),
        block(
            "memory", "runtime_state", "worker_journal_projection", "turn", 10,
            memory_text,
        ),
        block(
            "application_knowledge", "application_knowledge",
            "routed_application_knowledge", "session", 15,
            render_application_knowledge_context(application_knowledge),
            authoritative_for=("application_mechanics", "non_visual_postconditions"),
            freshness="session", coverage="complete",
        ) if application_knowledge.strip() else None,
    ]
    result = ContextCompressor(max_chars).apply(blocks)
    return WorkerContextProjection(
        text=render_context_blocks(result.kept, include_headers=False),
        report=result.to_report(label="tool_agent.worker.context"),
    )
