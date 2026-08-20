"""Bounded, journal-projected memory for one autonomous GUI Worker loop."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from gui_agent.context.blocks import (
    ContextBlock,
    ContextCompressor,
    render_context_blocks,
)
from gui_agent.core.tool_agent.contracts import MaterializedFrame, WorkerState


DEFAULT_WORKER_RECENT_K = 4
DEFAULT_WORKER_CONTEXT_MAX_CHARS = int(
    os.environ.get("TOOL_AGENT_WORKER_CONTEXT_MAX_CHARS") or 24_000
)

_RECENT_TRANSITION_LIMIT = 8


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
    """One immutable event in the Worker's semantic and execution timeline."""

    event_ref: str
    kind: str
    sequence: int = 0
    fact_type: Literal[
        "observation", "evidence", "claim", "commitment"
    ] | None = None
    key: str = ""
    status: Literal["active", "retracted", "completed"] | None = None
    lifetime: Literal["frame", "attempt", "task"] | None = None
    statement: str = ""
    frame_id: str = ""
    attempt_id: str = ""
    origin: Literal["worker", "runtime"] = "runtime"
    requires_integration: bool = False
    depends_on: tuple[str, ...] = ()
    supersedes: str = ""
    preserves_window: bool = False
    receipt_text: str = ""

    @property
    def fact_ref(self) -> str:
        return f"{self.fact_type}:{self.key}" if self.fact_type and self.key else ""


@dataclass
class WorkerJournal:
    """Fact stream owned by one GUI Worker invocation."""

    worker_id: str
    events: list[WorkerJournalEvent] = field(default_factory=list)
    executed_tools: set[str] = field(default_factory=set, repr=False)

    def _append(self, event: WorkerJournalEvent) -> WorkerJournalEvent:
        recorded = replace(event, sequence=len(self.events) + 1)
        self.events.append(recorded)
        return recorded

    def _latest_fact(self, fact_ref: str) -> WorkerJournalEvent | None:
        return next(
            (
                event for event in reversed(self.events)
                if event.kind == "memory_update" and event.fact_ref == fact_ref
            ),
            None,
        )

    def record_memory_updates(
        self,
        *,
        step: int,
        frame_id: str,
        state: WorkerState,
    ) -> tuple[WorkerJournalEvent, ...]:
        """Append one validated, ordered memory delta from a Worker decision."""

        pending: list[WorkerJournalEvent] = []

        def latest(fact_ref: str) -> WorkerJournalEvent | None:
            return next(
                (event for event in reversed(pending) if event.fact_ref == fact_ref),
                self._latest_fact(fact_ref),
            )

        for index, update in enumerate(state.memory_updates, start=1):
            fact_ref = f"{update.fact_type}:{update.key}"
            prior = latest(fact_ref)
            if prior is not None and prior.origin == "runtime":
                raise ValueError(
                    f"cannot modify Runtime-owned memory {fact_ref!r}"
                )
            if update.status != "active" and prior is None:
                raise ValueError(f"cannot {update.status} unknown memory {fact_ref!r}")
            dependencies = []
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
            pending.append(WorkerJournalEvent(
                event_ref=f"step:{step}:memory:{index}",
                kind="memory_update",
                fact_type=update.fact_type,
                key=update.key,
                status=update.status,
                lifetime=update.lifetime,
                statement=" ".join(update.statement.split()),
                frame_id=frame_id,
                attempt_id=self.worker_id,
                origin="worker",
                depends_on=tuple(dependencies),
                supersedes=prior.event_ref if prior is not None else "",
            ))
        if state.status == "executing":
            staged_events = [
                *self.events,
                *(
                    replace(event, sequence=len(self.events) + offset)
                    for offset, event in enumerate(pending, start=1)
                ),
            ]
            staged = WorkerJournal(worker_id=self.worker_id, events=staged_events)
            staged_view = build_worker_memory_view(staged, current_frame_id=frame_id)
            if not staged_view.active_commitments:
                raise ValueError(
                    "executing phase requires an active Commitment with valid dependencies"
                )
            if staged_view.pending_runtime_evidence:
                raise ValueError(
                    "executing phase requires authoritative runtime Evidence to be "
                    "integrated through Claim and Commitment dependencies"
                )
        return tuple(self._append(event) for event in pending)

    def record_runtime_input(
        self,
        *,
        key: str,
        statement: str,
        event_ref: str = "runtime-input:1",
        requires_integration: bool = False,
    ) -> WorkerJournalEvent:
        """Append authoritative task-lifetime evidence supplied by Runtime."""

        prior = self._latest_fact(f"evidence:{key}")
        return self._append(WorkerJournalEvent(
            event_ref=event_ref,
            kind="memory_update",
            fact_type="evidence",
            key=key,
            status="active",
            lifetime="task",
            statement=" ".join(statement.split()),
            attempt_id=self.worker_id,
            origin="runtime",
            requires_integration=requires_integration,
            supersedes=prior.event_ref if prior is not None else "",
        ))

    def record_runtime_result(
        self,
        *,
        step: int,
        result: Any,
        substep: int | None = None,
    ) -> WorkerJournalEvent | None:
        """Promote an explicit Runtime-owned action result into task Evidence."""

        if not isinstance(result, dict):
            return None
        statement = str(result.get("_runtime_memory_statement") or "").strip()
        if not statement:
            return None
        suffix = f"_{substep}" if substep is not None else ""
        return self.record_runtime_input(
            key=f"user_response_{step}{suffix}",
            statement=statement,
            event_ref=f"step:{step}{'.' + str(substep) if substep is not None else ''}:runtime-input",
            requires_integration=True,
        )

    def active_memory_statements(self, *, frame_id: str = "") -> tuple[str, ...]:
        view = build_worker_memory_view(self, current_frame_id=frame_id)
        return tuple(
            event.statement
            for event in (
                *view.current_observations,
                *view.accumulated_evidence,
                *view.established_claims,
                *view.active_commitments,
            )
        )

    def record_action_result(
        self,
        *,
        step: int,
        frame_id: str,
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
            intent = str(memory_args.get("description") or "").strip()
            intent_text = f"; intent={intent!r}" if intent else ""
            action_type = str(result.get("action_type") or "")
            action_text = f"; action={action_type}" if action_type else ""
            durable_text = (
                f"tool={tool}; flash verifier reported off_target{actual_text}"
                f"{action_text}{intent_text}; "
                "do not repeat the same point"
            )
        elif (
            result_status == "executed"
            and not is_no_effect
            and target_signal.get("status") == "on_target"
        ):
            actual = str(target_signal.get("actual_element") or "").strip()
            intent = str(memory_args.get("description") or "").strip()
            durable_text = (
                f"tool={tool}; action={str(result.get('action_type') or '')}; "
                f"target={actual!r}; intent={intent!r}; status=executed"
            )
        elif is_no_effect:
            action_type = (
                str(result.get("action_type") or "")
                if isinstance(result, dict)
                else ""
            )
            if action_type in {"type", "select_option", "clear_text"}:
                durable_text = (
                    f"tool={tool}; status=executed; visual effect unconfirmed; inspect "
                    "the current control value before deciding whether any retry is needed"
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
        receipt_text = (
            f"{durable_text}{args_text if is_no_effect else ''}"
            if durable_text
            else f"tool={tool}{args_text}; result={_bounded_json(memory_result, limit=260)}"
        )
        event_ref = (
            f"step:{step}.{substep}"
            if substep is not None
            else f"step:{step}"
        )
        self._append(WorkerJournalEvent(
            event_ref=event_ref,
            kind="candidate_commit" if candidate_commit else "action_receipt",
            frame_id=frame_id,
            attempt_id=self.worker_id,
            preserves_window=tool == "ask_user",
            receipt_text=receipt_text,
        ))

    def record_guard(
        self,
        *,
        step: int,
        repair_turn: int,
        tool: str,
        reason: str,
    ) -> None:
        self._append(WorkerJournalEvent(
            event_ref=f"step:{step}:guard:{repair_turn}",
            kind="feedback",
            attempt_id=self.worker_id,
            receipt_text=f"tool={tool}; {reason}",
        ))

@dataclass(frozen=True)
class WorkerMemoryView:
    worker_id: str
    current_observations: tuple[WorkerJournalEvent, ...] = ()
    accumulated_evidence: tuple[WorkerJournalEvent, ...] = ()
    established_claims: tuple[WorkerJournalEvent, ...] = ()
    active_commitments: tuple[WorkerJournalEvent, ...] = ()
    pending_runtime_evidence: tuple[WorkerJournalEvent, ...] = ()
    latest_gui_transition: tuple[WorkerJournalEvent, ...] = ()
    recent_receipts: tuple[WorkerJournalEvent, ...] = ()
    state_timeline: tuple[str, ...] = ()

    def render_prompt_section(self) -> str:
        projected = (
            *self.current_observations,
            *self.accumulated_evidence,
            *self.established_claims,
            *self.active_commitments,
        )
        fact_ref_by_event = {
            event.event_ref: event.fact_ref for event in projected
        }

        def dependency_refs(event: WorkerJournalEvent) -> tuple[str, ...]:
            return tuple(
                fact_ref_by_event.get(dependency, dependency)
                for dependency in event.depends_on
            )

        lines = [
            "## WorkerMemory (runtime-projected; not conversation history)",
            "This is a deterministic projection of an append-only event journal. "
            "A newer frame does not overwrite evidence with a different key; only an "
            "explicit update, expiry, or invalid dependency changes active memory. "
            "Evidence records what held at its source time; only the current frame proves "
            "present visibility or actionability.",
        ]
        sections = (
            ("Current-frame observations", self.current_observations),
            ("Accumulated evidence", self.accumulated_evidence),
            ("Established claims", self.established_claims),
            ("Active commitments", self.active_commitments),
        )
        for heading, events in sections:
            lines.append(f"### {heading}")
            lines.extend(
                f"- [t={event.sequence}; {event.fact_ref}; lifetime={event.lifetime}; "
                f"status={event.status}; source={event.frame_id or event.origin}] "
                f"{event.statement}"
                + (
                    f"; depends_on={json.dumps(dependency_refs(event), ensure_ascii=False)}"
                    if event.depends_on else ""
                )
                for event in events
            )
            if not events:
                lines.append("- None.")
        if self.latest_gui_transition:
            lines.extend([
                "### Latest GUI transition (immediately before the current frame)",
                "- Reconcile these ordered invocation receipts as one transaction with "
                "the current frame before retrying. Each receipt proves its action was "
                "invoked; the current frame shows the transaction's effect. 'Visual effect "
                "unconfirmed' is not an execution failure.",
            ])
            lines.extend(
                f"- [t={event.sequence}; {event.event_ref}] {event.receipt_text}"
                for event in self.latest_gui_transition
            )
        earlier_receipts = tuple(
            event for event in self.recent_receipts
            if event not in self.latest_gui_transition
        )
        if earlier_receipts:
            lines.append("### Earlier execution receipts")
            lines.extend(
                f"- [t={event.sequence}; {event.event_ref}] "
                f"{event.receipt_text}"
                for event in earlier_receipts
            )
        if self.pending_runtime_evidence:
            lines.append("### Authoritative evidence awaiting integration")
            lines.extend(
                f"- {event.fact_ref}: {event.statement}"
                for event in self.pending_runtime_evidence
            )
        lines.append("### Permitted next phase")
        if self.pending_runtime_evidence:
            lines.append(
                "- Before executing, establish a Claim that depends on each authoritative "
                "Evidence above and update the Commitment to depend on that Claim. The "
                "Claim must preserve the answer's value semantics; do not treat a hierarchy, "
                "range, or compound identifier as one leaf value. For path p0/.../pn under "
                "a current visible parent p0/.../pk, a single-name field receives exactly "
                "p(k+1). The answer establishes the path, not the current parent."
            )
        elif self.active_commitments:
            lines.append(
                "- Execute the active Commitment only with exact values established by its "
                "dependencies. A Commitment never resolves a descriptive user-owned role or "
                "authorizes an invented identifier; ask_user for any such missing value before "
                "creating, typing, or selecting it. An exact identifier does not prove its "
                "container exists; use the current frame to enter it or create an authorized "
                "missing container. Do not return to acquisition unless Runtime invalidates an "
                "exact dependency."
            )
        elif self.established_claims:
            lines.append(
                "- Preserve established Claims and acquire only evidence that is still "
                "unresolved; do not reopen a claimed boundary without invalidating evidence."
            )
        elif self.latest_gui_transition:
            lines.append(
                "- Reconcile the latest GUI transition with the current frame first. If a "
                "requested terminal commit exited its editor or form to the expected stable "
                "surface without an error or pending state, complete; do not restart merely "
                "because the application shows no success banner."
            )
        else:
            lines.append("- Continue the current attempt from verified current-frame evidence.")
        lines.append("### Memory update history (event-time status; current validity is above)")
        lines.extend(f"- {item}" for item in self.state_timeline)
        if not self.state_timeline:
            lines.append("- No memory transitions recorded.")
        if not any((projected, self.recent_receipts, self.state_timeline)):
            lines.extend(["### History", "- No prior Worker actions."])
        return "\n".join(lines)


def build_worker_memory_view(
    journal: WorkerJournal,
    *,
    current_frame_id: str = "",
    recent_k: int = DEFAULT_WORKER_RECENT_K,
) -> WorkerMemoryView:
    memory_events = [
        event for event in journal.events
        if event.kind == "memory_update" and event.fact_ref
    ]
    if not current_frame_id:
        current_frame_id = next(
            (event.frame_id for event in reversed(memory_events) if event.frame_id),
            "",
        )
    latest: dict[str, WorkerJournalEvent] = {}
    for event in memory_events:
        latest[event.fact_ref] = event
    def observation_is_current(event: WorkerJournalEvent) -> bool:
        if event.frame_id == current_frame_id:
            return True
        later_receipts = [
            item for item in journal.events
            if item.sequence > event.sequence
            and item.kind in {"action_receipt", "candidate_commit"}
        ]
        return bool(later_receipts) and all(
            item.preserves_window for item in later_receipts
        )

    candidates = [
        event for event in latest.values()
        if event.status == "active"
        and (
            event.lifetime != "frame"
            or observation_is_current(event)
        )
    ]
    valid_by_ref: dict[str, WorkerJournalEvent] = {}
    for event in sorted(candidates, key=lambda item: item.sequence):
        if all(dependency in valid_by_ref for dependency in event.depends_on):
            valid_by_ref[event.event_ref] = event
    valid = tuple(valid_by_ref.values())
    observations = tuple(
        event for event in valid if event.fact_type == "observation"
    )
    evidence = tuple(event for event in valid if event.fact_type == "evidence")
    claims = tuple(event for event in valid if event.fact_type == "claim")
    commitments = tuple(event for event in valid if event.fact_type == "commitment")
    integrated: set[str] = set()
    pending_dependencies = [
        dependency
        for commitment in commitments
        for dependency in commitment.depends_on
    ]
    while pending_dependencies:
        dependency = pending_dependencies.pop()
        if dependency in integrated:
            continue
        integrated.add(dependency)
        parent = valid_by_ref.get(dependency)
        if parent is not None:
            pending_dependencies.extend(parent.depends_on)
    pending_runtime = tuple(
        event for event in evidence
        if event.requires_integration and event.event_ref not in integrated
    )
    recent_limit = max(0, int(recent_k))
    receipt_by_ref = {
        event.event_ref: event for event in journal.events
        if (
            event.kind in {"action_receipt", "candidate_commit", "feedback"}
            and event.receipt_text
        )
    }
    receipts = sorted(receipt_by_ref.values(), key=lambda event: event.sequence)
    recent = tuple(receipts[-recent_limit:] if recent_limit else ())
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
    transition_anchor = receipts[-1] if receipts else None
    if not (
        transition_anchor
        and current_frame_id
        and transition_anchor.frame_id
        and transition_anchor.frame_id != current_frame_id
        and not transition_anchor.preserves_window
    ):
        transition_anchor = None
    latest_gui_transition = tuple(
        event for event in receipts
        if transition_anchor is not None
        and event.frame_id == transition_anchor.frame_id
        and not event.preserves_window
    )
    return WorkerMemoryView(
        worker_id=journal.worker_id,
        current_observations=observations,
        accumulated_evidence=evidence,
        established_claims=claims,
        active_commitments=commitments,
        pending_runtime_evidence=pending_runtime,
        latest_gui_transition=latest_gui_transition,
        recent_receipts=recent,
        state_timeline=timeline,
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
        # An unavailable structured-control inventory is not evidence that the
        # screenshot contains no controls. Omit the field entirely in pure-vision
        # contexts (including Android native surfaces) and let pixels drive.
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
    attempt_contract: str = "",
    task_contract: str = "",
    same_frame_feedback: dict[str, Any] | None = None,
    max_chars: int = DEFAULT_WORKER_CONTEXT_MAX_CHARS,
) -> WorkerContextProjection:
    """Build one capacity-managed context; the screenshot is supplied separately."""
    memory_text = memory.render_prompt_section()
    if len(memory_text) > max_chars:
        raise MemoryError(
            "memory_overflow: active WorkerMemory exceeds the context budget"
        )
    compact_frame = json.dumps(_frame_payload(
        frame,
        candidate_committed=any(
            event.kind == "candidate_commit" for event in memory.recent_receipts
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
        ContextBlock(
            id="tool_agent.worker.memory",
            source_type="runtime_state",
            source="worker_journal_projection",
            ttl="turn",
            budget="required",
            priority=10,
            content=memory_text,
        ),
        (
            ContextBlock(
                id="tool_agent.worker.original_task",
                source_type="runtime_task",
                source="original_task_goal",
                ttl="task",
                budget="required",
                priority=5,
                authoritative_for=("user_value_provenance",),
                freshness="task",
                coverage="complete",
                content=task_contract,
            )
            if task_contract.strip()
            else None
        ),
    ]
    result = ContextCompressor(max_chars).apply(blocks)
    return WorkerContextProjection(
        text=render_context_blocks(result.kept, include_headers=False),
        report=result.to_report(label="tool_agent.worker.context"),
    )


__all__ = [
    "DEFAULT_WORKER_CONTEXT_MAX_CHARS",
    "DEFAULT_WORKER_RECENT_K",
    "WorkerContextProjection",
    "WorkerJournal",
    "WorkerJournalEvent",
    "WorkerMemoryView",
    "build_worker_memory_view",
    "project_worker_context",
]
