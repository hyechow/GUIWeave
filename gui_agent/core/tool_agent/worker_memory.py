"""Bounded, journal-projected memory for one autonomous GUI Worker loop."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from gui_agent.context.blocks import (
    ContextBlock,
    ContextCompressor,
    render_context_blocks,
)
from gui_agent.core.tool_agent.contracts import (
    MaterializedFrame,
    WorkerState,
    positioned_rect,
)
from gui_agent.core.tool_agent.filter_state import diff_filter_sets


DEFAULT_WORKER_CONTEXT_MAX_CHARS = int(
    os.environ.get("TOOL_AGENT_WORKER_CONTEXT_MAX_CHARS") or 24_000
)


def _bounded_json(value: Any, *, limit: int = 1_200) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return text if len(text) <= limit else text[: limit - 1] + "…"


_SPATIAL_MEMORY_ARGS = {"x", "y", "to_x", "to_y", "target_ref", "description"}
_FRAME_SCOPED_FACT_RE = re.compile(
    r"\b(?:visible|off[- ]?screen|viewport|above|below|upper|lower|"
    r"top|bottom|left|right|center|current(?:ly)?|selected|checked|focused|"
    r"enabled|disabled|empty|open|closed|loading|showing|shows|displayed|value|status)\b"
    r"|\b(?:x|y)\s*[=:]\s*-?\d|\bat\s*\(?\s*-?\d+(?:\.\d+)?\s*[,/]\s*-?\d"
    r"|(?:可见|屏幕外|视口|折叠下方|上方|下方|左侧|右侧|顶部|底部|坐标|当前|已选择|"
    r"勾选|聚焦|启用|禁用|空白|打开|关闭|加载中|显示|状态|值为)",
    re.I,
)
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
    "traversal_action",
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


def _is_frame_scoped_fact(text: str) -> bool:
    """Whether a model observation belongs to the current rendered surface only."""

    return bool(_FRAME_SCOPED_FACT_RE.search(text))


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
    """Expose authoritative off-screen values without presenting action targets.

    A checked checkbox/radio carries its state in ``value`` (not
    ``selected_text``); without the second gate an off-screen inherited check
    is invisible everywhere in the Worker context (live failure: wizard
    advanced with an unseen Red checked alongside the requested colors).
    """

    return [
        {
            key: control[key]
            for key in ("kind", "label", *_SIGNIFICANT_EMPTY_CONTROL_FIELDS)
            if key in control
        }
        for control in controls
        if isinstance(control, dict)
        and control.get("in_viewport") is False
        and (
            any(key in control for key in _CHOICE_STATE_FIELDS)
            or str(control.get("value") or "").casefold() == "on"
        )
    ]


def _offscreen_status_messages(controls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep rendered feedback as evidence after scrolling moves it off-screen."""

    return [
        {
            key: control[key]
            for key in ("kind", "label", "value", "viewport_pos")
            if control.get(key) not in (None, "")
        }
        for control in controls
        if isinstance(control, dict)
        and control.get("kind") == "status_message"
        and control.get("in_viewport") is False
    ][:8]


_OFFSCREEN_ACTION_KINDS = frozenset({"button", "native_select", "section_toggle"})
_OFFSCREEN_CHOICE_KINDS = frozenset({
    "checkbox", "checkbox_input", "radio", "radio_group", "radio_input", "rating", "switch",
})
# Keeps the projection compact on long forms; sorted nearest-fold-first, so a
# form with more distinct off-fold actions than this drops only the farthest.
_OFFSCREEN_ACTION_LIMIT = 32


def _offscreen_action_controls(controls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Off-screen actionable controls with reveal coordinates, nearest fold first.

    ``_semantic_controls`` drops everything off-viewport, which made the
    reveal capability unusable — the Worker had no way to read the target
    rect. Ordinary row links (``a``) stay out because they are reached through
    collection regions; explicit page-traversal anchors remain revealable.
    Duplicate labels (mass-action buttons on every row) collapse to their
    nearest-fold instance so long forms stay compact.
    """

    def _fold_distance(control: dict[str, Any]) -> float:
        rect = control.get("rect") or {}
        y = rect.get("y")
        if not isinstance(y, (int, float)):
            return float("inf")
        return abs(float(y)) if float(y) < 0 else abs(float(y) - 1000.0)

    nearest: dict[tuple[str, str, str], dict[str, Any]] = {}
    for control in controls:
        if not (
            isinstance(control, dict)
            and control.get("in_viewport") is False
            and (
                str(control.get("kind") or "") in _OFFSCREEN_ACTION_KINDS
                or str(control.get("kind") or "") in _OFFSCREEN_CHOICE_KINDS
                and (
                    bool(control.get("options"))
                    or control.get("selected") is True
                    or str(control.get("value") or "").casefold() == "on"
                )
                or str(control.get("kind") or "") == "a"
                and (
                    str(control.get("traversal_action") or "").startswith("page_")
                    or str(control.get("value") or "").casefold().startswith("page ")
                )
            )
            and str(control.get("label") or "").strip()
            and positioned_rect(control) is not None
        ):
            continue
        key = tuple(str(control.get(field) or "").strip()
                    for field in ("kind", "label", "traversal_action"))
        if _fold_distance(control) < _fold_distance(nearest.get(key) or {}):
            nearest[key] = control
    candidates = sorted(nearest.values(), key=_fold_distance)
    return [
        {**{key: control[key] for key in (
            "kind", "label", "rect", "value", "traversal_action", "viewport_pos",
        ) if control.get(key)}, **(
            {"direct_capability": "select_option"}
            if str(control.get("kind") or "") in (
                _OFFSCREEN_ACTION_KINDS | _OFFSCREEN_CHOICE_KINDS
            ) and bool(control.get("options"))
            else {}
        )}
        for control in candidates[:_OFFSCREEN_ACTION_LIMIT]
    ]


@dataclass(frozen=True)
class WorkerJournalEvent:
    """One append-only event; only Runtime evidence may be durable."""

    event_ref: str
    kind: str
    frame_id: str = ""
    durable_text: str = ""
    narrative_text: str = ""
    belief_text: str = ""


TraversalPhase = Literal["open", "complete", "completed_elsewhere"]


@dataclass(frozen=True)
class InspectionTraversalProgress:
    """One structured page-inspection state and its model-facing explanation."""

    phase: TraversalPhase = "open"
    note: str = ""


@dataclass
class WorkerJournal:
    """Fact stream owned by one GUI Worker invocation."""

    worker_id: str
    events: list[WorkerJournalEvent] = field(default_factory=list)
    collection_context: str = ""
    collection_ref: str = ""
    last_scroll_no_effect: bool = False
    last_scroll_direction: str = ""
    last_scroll_collection_ref: str = ""
    last_scroll_point: tuple[float, float] | None = None
    established_fact_texts: set[str] = field(default_factory=set, repr=False)
    executed_tools: set[str] = field(default_factory=set, repr=False)
    confirmed_bindings: dict[str, str] = field(default_factory=dict, repr=False)
    # ReAct stop cue: consecutive observed frames that added no new collection rows.
    _collection_row_counts: dict[str, int] = field(default_factory=dict, repr=False)
    _collection_stable_frames: dict[str, int] = field(default_factory=dict, repr=False)
    _current_surface_key: tuple[str, str] | None = field(default=None, repr=False)
    _surface_coverage: dict[tuple[str, str], set[str]] = field(
        default_factory=dict, repr=False,
    )

    def _surface_traversal_complete(self, key: tuple[str, str]) -> bool:
        return self._surface_coverage.get(key, set()) >= {"scrolled", "start", "end"}

    def record_collection_stability(self, frame: MaterializedFrame) -> None:
        """Track consecutive frames that add no new rows to an accumulated collection."""
        self._current_surface_key = (frame.url, frame.title)
        coverage = self._surface_coverage.setdefault(self._current_surface_key, set())
        if frame.page_viewport.get("at_scroll_start") is True:
            coverage.add("start")
        if frame.page_viewport.get("at_scroll_end") is True:
            coverage.add("end")
        for collection in frame.collections:
            rid = collection.requirement_id
            row_count = collection.row_count
            if (
                row_count > 0
                and self._collection_row_counts.get(rid) == row_count
            ):
                self._collection_stable_frames[rid] = (
                    self._collection_stable_frames.get(rid, 0) + 1
                )
            else:
                self._collection_stable_frames[rid] = 0
            self._collection_row_counts[rid] = row_count

    def record_bound_confirmations(
        self,
        frame: MaterializedFrame,
        binding_values: dict[str, str],
    ) -> None:
        """Privately retain bound identities confirmed by rendered success feedback."""

        success = any(
            str(control.get("label") or "").casefold().startswith("success:")
            for control in frame.controls
            if isinstance(control, dict) and control.get("kind") == "status_message"
        )
        title = " ".join(frame.title.casefold().split())
        if not success or not title:
            return
        self.confirmed_bindings.update({
            name: " ".join(value.casefold().split())
            for name, value in binding_values.items()
            if " ".join(value.casefold().split()) == title
        })

    def collection_stability_note(self, frame: MaterializedFrame) -> str:
        """ReAct stop cue surfaced to the worker when revisits add no new rows."""
        notes: list[str] = []
        for collection in frame.collections:
            rid = collection.requirement_id
            stable = self._collection_stable_frames.get(rid, 0)
            if stable >= 2:
                notes.append(
                    f"{rid}: no new rows in the last {stable} observed frame(s); "
                    f"accumulated {collection.row_count} row(s)"
                )
        return "; ".join(notes)

    def traversal_progress(self, frame: MaterializedFrame) -> InspectionTraversalProgress:
        """Return boundary-backed page-inspection state and its compact explanation."""

        key = (frame.url, frame.title)
        if self._surface_traversal_complete(key):
            return InspectionTraversalProgress(
                phase="complete",
                note=(
                    "Inspection traversal is complete: this document's start and end boundaries "
                    "were both observed after scrolling. Further scroll is repetition, not "
                    "progress. A control absent throughout that coverage is absent from the "
                    "inspected document."
                ),
            )
        completed_surfaces = [
            surface_key for surface_key in self._surface_coverage
            if self._surface_traversal_complete(surface_key)
        ]
        if completed_surfaces:
            surfaces = "; ".join(
                surface_key[1]
                for surface_key in completed_surfaces[-3:]
            )
            return InspectionTraversalProgress(
                phase="completed_elsewhere",
                note=(
                    "Inspection traversal already completed for previously visited surface(s): "
                    f"{surfaces}. Do not reopen them; continue with remaining contract requirements."
                ),
            )
        return InspectionTraversalProgress()

    def observe_collection(self, frame: MaterializedFrame) -> str:
        """Return the collection ref carrying downward-scroll end evidence, if any."""

        if len(frame.visible_collection_regions) != 1:
            self.collection_ref = ""
            return ""
        self.collection_ref = (
            frame.collections[0].ref if len(frame.collections) == 1 else ""
        )
        region = frame.visible_collection_regions[0]
        bounds = region.get("bounds")
        context = str(region.get("caption") or "").strip()
        if isinstance(bounds, (list, tuple)) and len(bounds) == 4:
            top = float(bounds[1])
            anchors = [
                control for control in frame.controls
                if control.get("selected") is True
                and control.get("label")
                and isinstance(control.get("rect"), dict)
                and float(control["rect"].get("y") or 0)
                + float(control["rect"].get("h") or 0) / 2 <= top
            ]
            if anchors:
                context = str(max(
                    anchors,
                    key=lambda control: float(control["rect"].get("y") or 0),
                )["label"]).strip()
        if context:
            self.collection_context = context[:120]
        return (
            self.collection_ref
            if self.collection_ref == self.last_scroll_collection_ref
            and self.has_downward_scroll_end_evidence(frame)
            else ""
        )

    def has_downward_scroll_end_evidence(self, frame: MaterializedFrame) -> bool:
        """Whether facts show a no-effect downward scroll on the sole collection."""

        if not (
            self.last_scroll_no_effect
            and self.last_scroll_direction == "down"
            and self.last_scroll_point is not None
            and len(frame.visible_collection_regions) == 1
        ):
            return False
        bounds = frame.visible_collection_regions[0].get("bounds") or ()
        if len(bounds) != 4:
            return False
        x, y = self.last_scroll_point
        return (
            float(bounds[0]) <= x <= float(bounds[2])
            and float(bounds[1]) <= y <= float(bounds[3])
        )

    def record_established_fact(
        self, *, event_ref: str, frame_id: str = "", text: str,
    ) -> None:
        """Retain a model observation as bounded narrative, never durable evidence."""

        fact = " ".join(str(text or "").split())
        if not fact or fact in self.established_fact_texts:
            return
        self.established_fact_texts.add(fact)
        self.events.append(WorkerJournalEvent(
            event_ref=event_ref,
            kind="worker_observation",
            frame_id=frame_id,
            narrative_text=f"Worker-observed visual context: {fact}",
            belief_text="" if _is_frame_scoped_fact(fact) else fact,
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
            if result.get("action_type") == "scroll":
                self.last_scroll_no_effect = bool(result.get("no_effect"))
                self.last_scroll_direction = str(
                    result.get("direction") or args.get("direction") or ""
                )
                if self._current_surface_key and self.last_scroll_direction in {"up", "down"}:
                    self._surface_coverage.setdefault(
                        self._current_surface_key, set(),
                    ).add("scrolled")
                self.last_scroll_collection_ref = self.collection_ref
                x = args.get("x")
                y = args.get("y")
                self.last_scroll_point = (
                    float(x) if isinstance(x, (int, float)) else 500.0,
                    float(y) if isinstance(y, (int, float)) else 500.0,
                )
            else:
                self.collection_context = ""
                self.collection_ref = ""
                self.last_scroll_no_effect = False
                self.last_scroll_direction = ""
                self.last_scroll_collection_ref = ""
                self.last_scroll_point = None
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
                frame_id=frame_id,
                text=fact,
            )
        self.events.append(WorkerJournalEvent(
            event_ref=event_ref,
            kind="candidate_commit" if candidate_commit else "action_result",
            frame_id=frame_id,
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
    durable_facts: tuple[WorkerJournalEvent, ...]
    stable_beliefs: tuple[str, ...] = ()
    transient_context_omitted: bool = False

    def render_prompt_section(self) -> str:
        lines = [
            "## WorkerMemory (runtime-compacted current belief)",
            "This is a reduced working belief, not conversation history. The complete event "
            "journal remains audit-only. Current-frame controls are the sole authority for "
            "geometry, visibility, and spatial actions.",
        ]
        if self.durable_facts:
            lines.append("### Durable runtime facts")
            lines.extend(
                f"- [{event.event_ref}] {event.durable_text}"
                for event in reversed(self.durable_facts)
            )
        if self.stable_beliefs:
            lines.append(
                "### Stable semantic observations (historical prerequisite evidence only)"
            )
            lines.extend(f"- {text}" for text in self.stable_beliefs)
        if self.transient_context_omitted:
            lines.extend([
                "### Invalidated context",
                "- Historical geometry, visibility, viewport position, control state, and "
                "spatial action recommendations were omitted; read them only from Current "
                "MaterializedFrame.",
            ])
        if not self.durable_facts and not self.stable_beliefs:
            lines.extend(["### Current belief", "- No retained cross-frame state."])
        return "\n".join(lines)


def build_worker_memory_view(journal: WorkerJournal) -> WorkerMemoryView:
    durable_by_key: dict[tuple[str, str], WorkerJournalEvent] = {}
    for event in journal.events:
        if event.durable_text:
            durable_by_key[(event.kind, event.durable_text)] = event
    beliefs = tuple(dict.fromkeys(
        event.belief_text for event in journal.events if event.belief_text
    ))[-10:]
    return WorkerMemoryView(
        durable_facts=tuple(durable_by_key.values()),
        stable_beliefs=beliefs,
        transient_context_omitted=any(
            event.kind == "worker_observation"
            and bool(event.narrative_text)
            and not event.belief_text
            for event in journal.events
        ),
    )


def _frame_payload(
    frame: MaterializedFrame,
    *,
    candidate_committed: bool,
    collection_stability: str = "",
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
        filter_diff = diff_filter_sets(requested, applied)
        scope_blockers[requirement_id] = {
            "status": "unmet",
            "missing_applied_filters": filter_diff["missing"],
            "extra_applied_filters": filter_diff["extra"],
            "conflicting_applied_filters": filter_diff["conflicting"],
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
        "page_viewport": frame.page_viewport,
        "applied_filters": frame.applied_filters,
        "requirement_scopes": frame.requirement_scopes,
        "scope_blockers": scope_blockers,
        "observed_choice_state": _observed_choice_state(frame.controls),
        "offscreen_status_messages": _offscreen_status_messages(frame.controls),
        "offscreen_action_controls": _offscreen_action_controls(frame.controls),
        "candidate_set_state": candidate_state,
        "visible_collection_regions": frame.visible_collection_regions,
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
        "collection_stability": collection_stability,
    }


def render_worker_frame_context(
    frame: MaterializedFrame,
    *,
    candidate_committed: bool = False,
    collection_stability: str = "",
) -> str:
    """Render the current observation for both live decisions and replay."""

    return (
        f"## Current MaterializedFrame {frame.frame_id} (sole action authority)\n"
        "Runtime-owned collection/data values remain private. "
        "Visible collection cell text is exact current-frame evidence, but cells "
        "do not declare record boundaries; clipped cells may be incomplete. "
        "For every control rect, x/y are its normalized center coordinates; "
        "never add half of w/h to derive the action point.\n"
        + json.dumps(_frame_payload(
            frame,
            candidate_committed=candidate_committed,
            collection_stability=collection_stability,
        ), ensure_ascii=False)
    )


@dataclass(frozen=True)
class WorkerContextProjection:
    text: str
    report: dict[str, Any]


def render_current_frame_anchor(
    frame: MaterializedFrame,
    phase: TraversalPhase = "open",
) -> str:
    """Render the shared runtime/replay authority marker for one frame."""
    return (
        "## Current frame anchor (authoritative now)\n"
        "A terminal decision's required UI state must match this frame; historical "
        "or planned navigation cannot substitute. When inspection_traversal is "
        "complete, scrolling this document is no longer valid progress; when it is "
        "completed_elsewhere, do not reopen that completed surface.\n"
        + json.dumps({
            "frame_id": frame.frame_id,
            "url": frame.url,
            "title": frame.title,
            "inspection_traversal": phase,
        }, ensure_ascii=False)
    )


def project_worker_context(
    *,
    memory: WorkerMemoryView,
    frame: MaterializedFrame,
    attempt_contract: str = "",
    same_frame_feedback: dict[str, Any] | None = None,
    max_chars: int = DEFAULT_WORKER_CONTEXT_MAX_CHARS,
    collection_stability: str = "",
    traversal: InspectionTraversalProgress | None = None,
) -> WorkerContextProjection:
    """Build one capacity-managed context; the screenshot is supplied separately."""
    traversal = traversal or InspectionTraversalProgress()
    memory_text = memory.render_prompt_section()
    if traversal.note:
        memory_text = memory_text.replace(
            "### Stable semantic observations (historical prerequisite evidence only)",
            "### Completed historical prerequisites (not final completion)",
        )
        memory_text += (
            "\n### Traversal coverage\n- " + traversal.note
            + "\n### Pending terminal requirements\n"
            "- Any failure destination required by the immutable contract that does not "
            "match the current frame remains pending; terminal reporting is not permitted "
            "until it matches."
        )
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
            id="tool_agent.worker.current_frame",
            source_type="runtime_observation",
            source="materialized_frame",
            ttl="turn",
            budget="required",
            priority=20,
            freshness="turn",
            coverage="complete",
            content=render_worker_frame_context(
                frame,
                candidate_committed=any(
                    event.kind == "candidate_commit" for event in memory.durable_facts
                ),
                collection_stability=collection_stability,
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
            id="tool_agent.worker.current_frame_anchor",
            source_type="runtime_observation",
            source="materialized_frame_identity",
            ttl="turn",
            budget="required",
            priority=25,
            freshness="turn",
            authoritative_for=("current_surface",),
            content=render_current_frame_anchor(frame, traversal.phase),
        ),
    ]
    result = ContextCompressor(max_chars).apply(blocks)
    report = result.to_report(label="tool_agent.worker.context")
    return WorkerContextProjection(
        text=render_context_blocks(result.kept, include_headers=False),
        report=report,
    )


__all__ = [
    "DEFAULT_WORKER_CONTEXT_MAX_CHARS",
    "InspectionTraversalProgress",
    "WorkerContextProjection",
    "WorkerJournal",
    "WorkerJournalEvent",
    "WorkerMemoryView",
    "build_worker_memory_view",
    "project_worker_context",
    "render_current_frame_anchor",
    "render_worker_frame_context",
]
