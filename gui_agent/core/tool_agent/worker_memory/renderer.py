"""Prompt and context rendering from already-reduced Worker state."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from gui_agent.context.blocks import ContextBlock, ContextCompressor, render_context_blocks
from gui_agent.core.tool_agent.contracts import MaterializedFrame, WorkerSpec

from .context_state import (
    build_current_state,
    build_goal_contract,
    build_progress_snapshot,
)
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
    spec: WorkerSpec | None = None,
    completion_mode: str = "operator",
    application_knowledge: str = "",
    attempt_contract: str = "",
    same_frame_feedback: dict[str, Any] | None = None,
    max_chars: int = DEFAULT_WORKER_CONTEXT_MAX_CHARS,
) -> WorkerContextProjection:
    frame_payload = _frame_payload(
        frame,
        candidate_committed=any(
            event.kind == "candidate_commit" for event in memory.recent_receipts
        ),
    )
    progress = build_progress_snapshot(memory)
    current = build_current_state(
        frame=frame,
        observation=frame_payload,
        memory=memory,
        spec=spec,
        completion_mode=completion_mode,  # type: ignore[arg-type]
        same_frame_feedback=same_frame_feedback,
    )
    def block(
        block_id: str, source_type: str, source: str, ttl: str, priority: int,
        content: str, **metadata: Any,
    ) -> ContextBlock:
        return ContextBlock(
            id=f"tool_agent.worker.{block_id}", source_type=source_type,
            source=source, ttl=ttl, budget="required", priority=priority,
            content=content, **metadata,
        )

    static_content = (
        "## Static Rules\n"
        "The system protocol and capability contracts are authoritative. Application "
        "knowledge below contains stable mechanics, never current task state."
        + (
            "\n\n" + render_application_knowledge_context(application_knowledge)
            if application_knowledge.strip() else ""
        )
    )
    goal_content = (
        "## Goal Contract\n"
        + json.dumps(build_goal_contract(spec).to_dict(), ensure_ascii=False)
        if spec is not None else
        "## Goal Contract\n" + attempt_contract
    )
    blocks = [
        block(
            "01_static_rules", "runtime_contract", "static_rules", "session", 5,
            static_content,
        ),
        block(
            "02_goal_contract", "runtime_contract", "worker_spec", "turn", 10,
            goal_content, authoritative_for=("goal", "output_contract", "approach"),
            freshness="turn", coverage="complete",
        ),
        block(
            "03_historical_progress", "runtime_state", "progress_reducer", "turn", 15,
            "## Historical Progress\n"
            + json.dumps(progress.to_dict(), ensure_ascii=False),
            freshness="turn", coverage="complete",
        ),
        block(
            "04_current_state", "runtime_observation", "current_state", "turn", 20,
            "## Current State\n"
            "This final layer is most recent. For every control rect, x/y are its normalized "
            "center; do not add half of w/h.\n"
            + json.dumps(current.to_dict(), ensure_ascii=False),
            freshness="turn", coverage="complete",
        ),
    ]
    result = ContextCompressor(max_chars).apply(blocks)
    return WorkerContextProjection(
        text=render_context_blocks(result.kept, include_headers=False),
        report=result.to_report(label="tool_agent.worker.context"),
    )
