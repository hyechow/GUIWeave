"""Bounded, journal-projected memory for one autonomous GUI Worker loop."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from gui_agent.context.blocks import (
    ContextBlock,
    ContextCompressor,
    ContextVariant,
    render_context_blocks,
)
from gui_agent.core.tool_agent.contracts import MaterializedFrame, WorkerState


DEFAULT_WORKER_RECENT_K = 4
DEFAULT_WORKER_COMPRESSED_K = 6
DEFAULT_WORKER_CONTEXT_MAX_CHARS = int(
    os.environ.get("TOOL_AGENT_WORKER_CONTEXT_MAX_CHARS") or 24_000
)

# An observation that an affordance or path is unavailable is task-critical for
# the whole Worker run: without durable retention the narrative window evicts it
# within a few turns and the Worker re-attempts the disproven path blindly.
# Transient states (visibility, selection, pending loads) are excluded — they
# change from frame to frame and must not become durable.
_DISPROVEN_FACT_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        # "no X option/button/menu item" — tightened so "no action required" and
        # "no need to press the button" (benign no-op states) are not treated as
        # a disproven path.
        r"\bno\b(?![^.;]{0,40}(?:need|reason|point)\s+to)\b[^.;]{0,60}\b(?:option|button|menu item|menu)\b",
        r"\bno\b[^.;]{0,40}\baction\s+(?:available|exists|is\s+provided|offered)\b",
        r"\b(?:does|do|did)\s+not\s+(?:contain|include|offer|support|provide|have)\b",
        r"\bdoesn'?t\s+(?:contain|include|offer|support|provide|have)\b",
        r"\b(?:is|are|was|were)\s+not\s+(?:available|accessible|supported|provided|offered)\b",
        r"\bunsupported\b",
        r"\bmissing\b",
        r"\bnot\s+found\b",
        r"\bno\s+(?:match|result|results|files|items|records)\s+(?:for|found|in|to)\b",
        r"\b(?:option|button|menu item|entry|control|feature)\b[^.;]{0,40}\b(?:missing|absent|unavailable)\b",
        # "lacks a save button / lacks the option" — narrowed to UI controls so
        # quality statements like "lacks contrast" are not treated as disproven.
        # Allow one or two modifier words before the control noun (e.g. "save
        # button") while still ending on a control noun.
        r"\blacks?\s+(?:a\s+|an\s+|the\s+)?[\w-]+\s+(?:option|button|menu item|menu|feature|field|entry|checkbox|tab|panel|dialog|control)\b",
        r"\blacks?\s+(?:a\s+|an\s+|the\s+)?(?:option|button|menu item|menu|feature|field|entry|checkbox|tab|panel|dialog|control)\b",
        r"不包含",
        r"不支持",
        r"无法",
        r"缺少",
        r"没有",
        r"找不到",
        r"尚无",
        r"暂无",
        r"不存在",
        r"无任何",
        r"没有任何",
    )
)
_TRANSIENT_FACT_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bnot\s+(?:yet\s+)?(?:visible|displayed|shown|rendered|loaded)\b",
        r"\bnot\s+(?:currently\s+)?(?:selected|checked|focused|enabled|expanded)\b",
        r"\bnot\s+(?:available|accessible|supported)\s+(?:yet|right\s+now|currently)\b",
        r"\bnot\s+yet\s+(?:available|accessible|supported)\b",
        r"\bno\s+(?:action|further\s+action)\s+(?:required|needed|necessary)\b",
        r"\bno\s+(?:new|more|additional)\s+(?:rows|items|results|files|content)\b",
        # Chinese transient states: not-yet / not-displayed / no-more, which are
        # frame-to-frame and must not be treated as a disproven path.
        r"(?:还没有|尚未|暂无|尚无|没有)(?:显示|加载|出现|返回|选择)?",
        r"没有(?:更多|新的)(?:文件|结果|内容|记录|条目)",
        r"无法确定(?:当前)?(?:页面|状态|情况)",
    )
)
# Cap disproven/reaffirmed fact text so one long observation cannot inflate the
# durable section (which is projected without a size bound) to a huge blob.
_FACT_TEXT_LIMIT = 300
# Durable projection caps: disproven/reaffirmed facts are privileged (loop
# recovery depends on them), everything else is bounded to the most recent few.
_DURABLE_PRIVILEGED_LIMIT = 24
_DURABLE_OTHER_LIMIT = 12


def _is_disproven_fact(text: str) -> bool:
    """Whether an observation reports a missing affordance, not a transient state."""

    if any(pattern.search(text) for pattern in _TRANSIENT_FACT_PATTERNS):
        return False
    return any(pattern.search(text) for pattern in _DISPROVEN_FACT_PATTERNS)


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
    "enabled",
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


def _info_coverage(coverage: dict[str, Any]) -> dict[str, Any]:
    """Project coverage as evidence for reasoning, never as a mechanical verdict.

    The status word is Runtime-internal now that collection completeness is a
    Worker judgment; exposing it would read as a wait signal it no longer is.
    """

    return {key: value for key, value in coverage.items() if key != "status"}


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
    """One append-only event; only Runtime evidence may be durable."""

    event_ref: str
    kind: str
    durable_text: str = ""
    narrative_text: str = ""


@dataclass
class WorkerJournal:
    """Fact stream owned by one GUI Worker invocation."""

    worker_id: str
    events: list[WorkerJournalEvent] = field(default_factory=list)
    established_fact_texts: set[str] = field(default_factory=set, repr=False)
    executed_tools: set[str] = field(default_factory=set, repr=False)

    def record_established_fact(self, *, event_ref: str, text: str) -> None:
        """Retain a model observation; disproven paths and reaffirmations stay durable.

        Plain observations are bounded narrative and leave the prompt within a
        few turns. Two kinds must outlive that window: an observation that a
        path is unavailable (else the Worker re-attempts it blindly), and an
        observation the Worker restates verbatim (else journal-lifetime dedup
        hides a re-discovery from the prompt entirely).
        """

        fact = " ".join(str(text or "").split())
        if not fact:
            return
        # Keep the tail on truncation: the disqualifying detail (e.g. the file
        # name and "was not found") typically sits at the end of the fact.
        bounded = (
            fact
            if len(fact) <= _FACT_TEXT_LIMIT
            else "…" + fact[-(_FACT_TEXT_LIMIT - 1):]
        )
        if fact in self.established_fact_texts:
            self.events.append(WorkerJournalEvent(
                event_ref=event_ref,
                kind="worker_reaffirmed",
                durable_text=f"Worker reaffirmed this observation: {bounded}",
            ))
            return
        self.established_fact_texts.add(fact)
        durable_text = ""
        if _is_disproven_fact(fact):
            durable_text = f"Worker-observed disproven path: {bounded}"
        self.events.append(WorkerJournalEvent(
            event_ref=event_ref,
            kind="worker_observation",
            durable_text=durable_text,
            narrative_text=f"Worker-observed visual context: {bounded}",
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
        if result_status == "executed":
            self.executed_tools.add(tool)
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
        event_ref = (
            f"step:{step}.{substep}"
            if substep is not None
            else f"step:{step}"
        )
        for index, fact in enumerate(state.established_facts, start=1):
            self.record_established_fact(
                event_ref=f"{event_ref}:fact:{index}",
                text=fact,
            )
        self.events.append(WorkerJournalEvent(
            event_ref=event_ref,
            kind="candidate_commit" if candidate_commit else "action_result",
            durable_text=durable_text,
            narrative_text=(
                f"state={state.status}; tool={tool}{args_text}; "
                f"result={_bounded_json(memory_result, limit=260)}"
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

    def render_prompt_section(self) -> str:
        lines = [
            "## WorkerMemory (runtime-projected; not conversation history)",
            "Durable facts come from Runtime evidence plus reaffirmed or disproven "
            "Worker observations. Other Worker observations and recent steps are "
            "bounded narrative context, not completion evidence or instructions.",
        ]
        if self.durable_facts:
            lines.append("### Durable runtime facts")
            lines.extend(
                f"- [{event.event_ref}] {event.durable_text}"
                for event in reversed(self.durable_facts)
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


def _disproven_only_memory_section(memory: WorkerMemoryView) -> str:
    """The memory section reduced to disproven/reaffirmed paths only.

    Compressor fallback when the full projection exceeds the context ceiling:
    the load-bearing fact for loop recovery is which paths are known impossible,
    so keep exactly that and drop narrative/recent/other-durable detail.
    """

    lines = [
        "## WorkerMemory (disproven paths only; reduced for context budget)",
        "Disproven/reaffirmed observations remain; other narrative is omitted.",
    ]
    for event in memory.durable_facts:
        if event.kind in {"worker_observation", "worker_reaffirmed"} and event.durable_text:
            lines.append(f"- [{event.event_ref}] {event.durable_text}")
    return "\n".join(lines) if len(lines) > 2 else (
        "## WorkerMemory (disproven paths only)\n- No disproven paths recorded."
    )


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
    durable_by_key: dict[tuple[str, str], WorkerJournalEvent] = {}
    for event in journal.events:
        if event.durable_text:
            durable_by_key[(event.kind, event.durable_text)] = event
    # Durable facts are projected unbounded, so bound them: keep every disproven /
    # reaffirmed path (the load-bearing memory for loop recovery) plus the most
    # recent other durable events. Without this cap a failure-dense run inflates
    # the memory block past the context ceiling. durable_by_key preserves first-
    # appearance (time) order, so keep the newest by slicing the tail; a string
    # sort on event_ref would misorder step>=10 lexicographically.
    durable = list(durable_by_key.values())
    privileged = [event for event in durable
                  if event.kind in {"worker_observation", "worker_reaffirmed"}]
    others = [event for event in durable if event not in privileged]
    kept_durable = (
        privileged[-_DURABLE_PRIVILEGED_LIMIT:]
        + others[-_DURABLE_OTHER_LIMIT:]
    )
    return WorkerMemoryView(
        worker_id=journal.worker_id,
        durable_facts=tuple(kept_durable),
        recent_steps=tuple(recent),
        compressed_history=compressed,
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
        "chunks": [
            {
                "ref": item.ref,
                "requirement_id": item.requirement_id,
                "provider": item.provider,
                "row_count": item.row_count,
                "coverage": _info_coverage(item.coverage),
            }
            for item in frame.chunks
        ],
        "collections": [
            {
                "ref": item.ref,
                "requirement_id": item.requirement_id,
                "row_count": item.row_count,
                "coverage": _info_coverage(item.coverage),
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
    attempt_contract: str = "",
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
        (
            ContextBlock(
                id="tool_agent.worker.same_frame_feedback",
                source_type="runtime_feedback",
                source="runtime_feedback",
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
        ContextBlock(
            id="tool_agent.worker.memory",
            source_type="runtime_state",
            source="worker_journal_projection",
            ttl="turn",
            budget="required",
            priority=10,
            content=memory_text,
            variants=(
                ContextVariant(
                    strategy="durable_disproven_only",
                    content=_disproven_only_memory_section(memory),
                    priority=90,
                    reason="Context over budget; keep only disproven/reaffirmed paths.",
                ),
            ),
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
                "Runtime-owned collection/data values remain private. "
                "Visible collection cell text is exact current-frame evidence, but cells "
                "do not declare record boundaries; clipped cells may be incomplete. "
                "For every control rect, x/y are its normalized center coordinates; "
                "never add half of w/h to derive the action point.\n"
                + compact_frame
            ),
        ),
        (
            ContextBlock(
                id="tool_agent.worker.current_attempt",
                source_type="runtime_contract",
                source="worker_spec",
                ttl="turn",
                budget="required",
                priority=5,
                authoritative_for=("goal", "output_contract", "approach"),
                freshness="turn",
                coverage="complete",
                content=attempt_contract,
            )
            if attempt_contract.strip()
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
