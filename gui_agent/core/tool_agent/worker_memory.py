"""Bounded, journal-projected memory for one autonomous GUI Worker loop."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from gui_agent.context.blocks import (
    ContextBlock,
    ContextCompressor,
    render_context_blocks,
)
from gui_agent.core.tool_agent.contracts import MaterializedFrame, WorkerState


DEFAULT_WORKER_RECENT_K = 4
DEFAULT_WORKER_COMPRESSED_K = 6
DEFAULT_WORKER_CONTEXT_MAX_CHARS = int(
    os.environ.get("TOOL_AGENT_WORKER_CONTEXT_MAX_CHARS") or 24_000
)


def _bounded_json(value: Any, *, limit: int = 1_200) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return text if len(text) <= limit else text[: limit - 1] + "…"


_SPATIAL_MEMORY_ARGS = {"x", "y", "to_x", "to_y", "target_ref"}
_CONTROL_PROMPT_FIELDS = (
    "kind",
    "label",
    "placeholder",
    "value",
    "selected",
    "selection_mode",
    "selected_text",
    "selected_text_primary",
    "options",
    "focused",
    "required",
    "is_filter",
    "query_action",
    "form_action",
    "is_datepicker",
    "group_index",
    "group_field",
    "row_values",
    "in_viewport",
    "viewport_pos",
    "occluded",
    "rect",
)
_CHOICE_STATE_FIELDS = (
    "selected_text",
    "selected_text_primary",
)
_SIGNIFICANT_EMPTY_CONTROL_FIELDS = ("value", *_CHOICE_STATE_FIELDS)
_RESULT_MEMORY_FIELDS = (
    "status",
    "action_type",
    "no_effect",
    "kind",
    "ref",
    "requirement_id",
    "row_count",
    "coverage",
    "summary",
    "reason",
    "error",
    "recovery",
    "platform_feedback",
    "target_signal",
)


def _semantic_action_args(args: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in args.items()
        if key not in _SPATIAL_MEMORY_ARGS
    }


def _semantic_result(result: Any) -> Any:
    if not isinstance(result, dict):
        return result
    return {
        key: result[key]
        for key in _RESULT_MEMORY_FIELDS
        if key in result and result[key] not in (None, "", [], {})
    }


def _semantic_controls(controls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Expose controls that can be grounded in the current screenshot."""

    return [
        {
            key: control[key]
            for key in _CONTROL_PROMPT_FIELDS
            if key in control
            and (
                key in _SIGNIFICANT_EMPTY_CONTROL_FIELDS
                or control[key] not in (None, "", [], {})
            )
        }
        for control in controls
        if isinstance(control, dict)
        and control.get("in_viewport") is not False
    ]


def _observed_choice_state(controls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Expose authoritative off-screen values without presenting action targets."""

    return [
        {
            key: control[key]
            for key in ("kind", "label", *_SIGNIFICANT_EMPTY_CONTROL_FIELDS)
            if key in control
        }
        for control in controls
        if isinstance(control, dict)
        and control.get("in_viewport") is False
        and any(key in control for key in _CHOICE_STATE_FIELDS)
    ]


@dataclass(frozen=True)
class WorkerJournalEvent:
    """One append-only Worker event; durable text must come from runtime facts."""

    event_ref: str
    kind: str
    durable_text: str = ""
    narrative_text: str = ""
    pending_subgoal: str = ""


@dataclass
class WorkerJournal:
    """Fact stream owned by one GUI Worker invocation."""

    worker_id: str
    events: list[WorkerJournalEvent] = field(default_factory=list)

    def record_replan(self, *, attempt: int) -> None:
        self.events.append(WorkerJournalEvent(
            event_ref=f"replan:{attempt}",
            kind="subgoal_replan",
            durable_text=(
                f"local GUI subgoal attempt {attempt} started with prior runtime "
                "experience retained"
            ),
        ))

    def record_patch(
        self,
        *,
        step: int,
        patch_turn: int,
        payload: dict[str, Any],
    ) -> None:
        status = str(payload.get("status") or "unknown")
        action = payload.get("action") if isinstance(payload.get("action"), dict) else {}
        name = str(action.get("name") or "unknown")
        capability = str(action.get("capability") or "unknown")
        error = str(payload.get("error") or "")
        detail = f"; error={error}" if error else ""
        self.events.append(WorkerJournalEvent(
            event_ref=f"step:{step}:patch:{patch_turn}",
            kind="action_patch",
            durable_text=(
                f"action patch {status}: {name} ({capability}){detail}"
            ),
        ))

    def record_turn(
        self,
        *,
        step: int,
        frame_id: str,
        state: WorkerState,
        tool: str,
        args: dict[str, Any],
        result: Any,
        substep: int | None = None,
    ) -> None:
        del frame_id  # Frame identity and coordinates do not improve later decisions.
        memory_args = _semantic_action_args(args)
        memory_result = _semantic_result(result)
        result_status = (
            str(result.get("status") or "")
            if isinstance(result, dict)
            else ""
        )
        result_kind = (
            str(result.get("kind") or "")
            if isinstance(result, dict)
            else ""
        )
        is_exception = isinstance(result, dict) and bool(result.get("error"))
        is_no_effect = isinstance(result, dict) and bool(result.get("no_effect"))
        candidate_commit = isinstance(result, dict) and bool(result.get("candidate_commit"))
        target_signal = (
            result.get("target_signal")
            if isinstance(result, dict)
            and isinstance(result.get("target_signal"), dict)
            else {}
        )
        is_result_ref = result_kind == "result" or (
            isinstance(result, dict) and str(result.get("ref") or "").startswith("result:")
        )
        durable_text = ""
        if candidate_commit:
            durable_text = "candidate selection commit produced a confirmed transition"
        elif is_exception or result_status in {"error", "failed"}:
            durable_text = (
                f"tool={tool}; failure={_bounded_json(memory_result, limit=420)}"
            )
        elif target_signal.get("status") == "off_target":
            actual = str(target_signal.get("actual_element") or "").strip()
            actual_text = f"; marker landed on {actual!r}" if actual else ""
            durable_text = (
                f"tool={tool}; flash verifier reported off_target{actual_text}; "
                "do not repeat the same point"
            )
        elif is_no_effect:
            action_type = (
                str(result.get("action_type") or "")
                if isinstance(result, dict)
                else ""
            )
            if action_type in {"type", "select_option", "clear_text"}:
                durable_text = (
                    f"tool={tool}; effect unconfirmed; inspect the current control value "
                    "before deciding whether any retry is needed"
                )
            else:
                durable_text = f"tool={tool}; runtime reported no_effect"
        elif is_result_ref:
            durable_text = f"tool={tool}; result={_bounded_json(memory_result, limit=420)}"
        args_text = (
            f"; args={_bounded_json(memory_args, limit=220)}"
            if memory_args
            else ""
        )
        self.events.append(WorkerJournalEvent(
            event_ref=(
                f"step:{step}.{substep}"
                if substep is not None
                else f"step:{step}"
            ),
            kind="candidate_commit" if candidate_commit else "action_result",
            durable_text=durable_text,
            narrative_text=(
                f"state={state.status}; tool={tool}{args_text}; "
                f"result={_bounded_json(memory_result, limit=260)}; "
                f"summary={state.summary[:240]}; next={state.next_instruction[:200]}"
            ),
            pending_subgoal=(
                state.open_gaps[0]
                if state.open_gaps
                else state.next_instruction
            ),
        ))

    def record_guard(
        self,
        *,
        step: int,
        repair_turn: int,
        tool: str,
        reason: str,
    ) -> None:
        self.events.append(WorkerJournalEvent(
            event_ref=f"step:{step}:guard:{repair_turn}",
            kind="action_guard",
            durable_text=f"tool={tool}; {reason}",
        ))


@dataclass(frozen=True)
class WorkerMemoryView:
    worker_id: str
    durable_facts: tuple[WorkerJournalEvent, ...]
    recent_steps: tuple[WorkerJournalEvent, ...]
    compressed_history: tuple[str, ...]
    pending_subgoal: str = ""

    def render_prompt_section(self) -> str:
        lines = [
            "## WorkerMemory (runtime-projected; not conversation history)",
            "Only runtime results below are facts. State summaries are narrative guidance, "
            "not completion evidence.",
        ]
        if self.pending_subgoal:
            lines.extend(["### Pending subgoal", f"- {self.pending_subgoal}"])
        if self.durable_facts:
            lines.append("### Durable runtime facts")
            lines.extend(
                f"- [{event.event_ref}] {event.durable_text}"
                for event in self.durable_facts
            )
        if self.compressed_history:
            lines.append("### Older narrative summaries")
            lines.extend(f"- {text}" for text in self.compressed_history)
        if self.recent_steps:
            lines.append("### Recent narrative steps")
            lines.extend(
                f"- [{event.event_ref}] {event.narrative_text}"
                for event in self.recent_steps
                if event.narrative_text
            )
        if not self.durable_facts and not self.recent_steps:
            lines.extend(["### History", "- No prior Worker actions."])
        return "\n".join(lines)


def build_worker_memory_view(
    journal: WorkerJournal,
    *,
    recent_k: int = DEFAULT_WORKER_RECENT_K,
    compressed_k: int = DEFAULT_WORKER_COMPRESSED_K,
) -> WorkerMemoryView:
    narrative = [event for event in journal.events if event.narrative_text]
    recent_limit = max(0, int(recent_k))
    recent = narrative[-recent_limit:] if recent_limit else []
    older = narrative[:-recent_limit] if recent_limit else narrative
    compressed_limit = max(0, int(compressed_k))
    compressed_events = older[-compressed_limit:] if compressed_limit else []
    compressed = tuple(
        f"[{event.event_ref}] {event.narrative_text}"
        for event in compressed_events
    )
    pending = recent[-1].pending_subgoal if recent else ""
    durable_by_key: dict[tuple[str, str], WorkerJournalEvent] = {}
    for event in journal.events:
        if event.durable_text:
            durable_by_key[(event.kind, event.durable_text)] = event
    return WorkerMemoryView(
        worker_id=journal.worker_id,
        durable_facts=tuple(durable_by_key.values()),
        recent_steps=tuple(recent),
        compressed_history=compressed,
        pending_subgoal=pending,
    )


def _frame_payload(
    frame: MaterializedFrame, *, candidate_committed: bool,
) -> dict[str, Any]:
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
                key
                for key in set(requested).intersection(applied)
                if requested[key] != applied[key]
            ),
            "instruction": (
                "Resolve every missing, conflicting, or extra applied filter through "
                "the visible UI before collecting or completing."
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
        "task_reference_time": frame.platform_time,
        "url": frame.url,
        "title": frame.title,
        "applied_filters": frame.applied_filters,
        "requirement_scopes": frame.requirement_scopes,
        "scope_blockers": scope_blockers,
        "observed_choice_state": _observed_choice_state(frame.controls),
        "candidate_set_state": candidate_state,
        "structured_surfaces": frame.structured_surfaces,
        "chunks": [
            {
                "ref": item.ref,
                "requirement_id": item.requirement_id,
                "provider": item.provider,
                "row_count": item.row_count,
                "coverage": item.coverage,
            }
            for item in frame.chunks
        ],
        "collections": [
            {
                "ref": item.ref,
                "requirement_id": item.requirement_id,
                "row_count": item.row_count,
                "coverage": item.coverage,
            }
            for item in frame.collections
        ],
        "missing_requirements": frame.missing_requirements,
        "controls": _semantic_controls(frame.controls),
    }


@dataclass(frozen=True)
class WorkerContextProjection:
    text: str
    report: dict[str, Any]


def project_worker_context(
    *,
    memory: WorkerMemoryView,
    frame: MaterializedFrame,
    same_frame_feedback: dict[str, Any] | None = None,
    max_chars: int = DEFAULT_WORKER_CONTEXT_MAX_CHARS,
) -> WorkerContextProjection:
    """Build one capacity-managed context; the screenshot is supplied separately."""
    memory_text = memory.render_prompt_section()
    compact_frame = json.dumps(_frame_payload(
        frame,
        candidate_committed=any(
            event.kind == "candidate_commit" for event in memory.durable_facts
        ),
    ), ensure_ascii=False)
    blocks = [
        ContextBlock(
            id="tool_agent.worker.memory",
            source_type="runtime_state",
            source="worker_journal_projection",
            ttl="turn",
            budget="required",
            priority=10,
            content=memory_text,
        ),
        ContextBlock(
            id="tool_agent.worker.current_frame",
            source_type="runtime_observation",
            source="materialized_frame",
            ttl="turn",
            budget="required",
            priority=20,
            freshness="turn",
            coverage="complete",
            content=(
                "## Current MaterializedFrame (compact semantic projection)\n"
                "Values remain private; descriptors and controls only. "
                "For every control rect, x/y are its normalized center coordinates; "
                "never add half of w/h to derive the action point.\n"
                + compact_frame
            ),
        ),
        (
            ContextBlock(
                id="tool_agent.worker.same_frame_feedback",
                source_type="runtime_feedback",
                source="action_patch",
                ttl="turn",
                budget="required",
                priority=5,
                content=(
                    "## Same-frame runtime feedback\n"
                    + json.dumps(same_frame_feedback, ensure_ascii=False, default=str)
                ),
            )
            if same_frame_feedback
            else None
        ),
    ]
    result = ContextCompressor(max_chars).apply(blocks)
    return WorkerContextProjection(
        text=render_context_blocks(result.kept, include_headers=False),
        report=result.to_report(label="tool_agent.worker.context"),
    )


__all__ = [
    "DEFAULT_WORKER_COMPRESSED_K",
    "DEFAULT_WORKER_CONTEXT_MAX_CHARS",
    "DEFAULT_WORKER_RECENT_K",
    "WorkerContextProjection",
    "WorkerJournal",
    "WorkerJournalEvent",
    "WorkerMemoryView",
    "build_worker_memory_view",
    "project_worker_context",
]
