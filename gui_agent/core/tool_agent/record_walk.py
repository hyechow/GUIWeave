"""Deterministic linked-detail record walk.

When a collector frame is a record editor participating in an active
detail-resolution assembly, the Runtime — not the Worker policy — advances
through sibling records via the semantic ``record_next`` traversal control.
Each driver step is one crank of a mechanical loop the assembly mechanism
already tracks: crediting happened during frame materialization, so the only
actuation needed is activating the next-record control.

The Worker keeps every genuinely ambiguous decision: entering the walk,
crossing list pages, and recovering when the walk stalls (no frame progress
after acting) or exhausts its step budget. Escalation is always the safe
direction — a false stall costs one Worker turn, a wrong deterministic tap
costs the run.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any
from urllib.parse import urlsplit

from gui_agent.core.tool_agent.action_guard import progress_signature
from gui_agent.core.tool_agent.contracts import MaterializedFrame, positioned_rect

# No-progress frames tolerated before the walk hands control back to the Worker.
# Aligned with WorkerActionCircuitBreaker's repetition threshold.
MAX_WALK_STALLS = 2
# Consecutive walk steps that produced no new credit before yielding. Pressing
# on through already-resolved records, non-candidate records, or rows whose
# detail genuinely won't read all look identical to the driver: no credit.
# Either way the Worker must re-aim at the exact next_unresolved identity
# instead of re-walking a resolved prefix.
MAX_NO_CREDIT_STREAK = 2


@dataclass
class RecordWalkState:
    """Transient driver state, reset whenever the Worker holds control."""

    last_progress: str = ""
    last_resolved: tuple[int, ...] = ()
    stalls: int = 0
    no_credit_streak: int = 0
    steps: int = 0
    engaged: bool = False
    last_page_candidates: int = -1
    last_open_id: str = ""

    def reset(self) -> None:
        for field in fields(self):
            setattr(self, field.name, field.default)


@dataclass(frozen=True)
class RecordWalkStep:
    control: dict[str, Any]
    resolved: tuple[int, ...]
    candidate_records: int
    reason: str
    navigation_url: str = ""


def same_origin_navigation(frame: MaterializedFrame, candidate: Any) -> str:
    url = str(candidate or "").strip()
    current = urlsplit(frame.url)
    target = urlsplit(url)
    if (
        current.scheme in {"http", "https"}
        and current.netloc
        and target.scheme in {"http", "https"}
        and target.netloc
        and (target.scheme, target.netloc) == (current.scheme, current.netloc)
    ):
        return url
    return ""


def walk_detail_scope(frame: MaterializedFrame) -> dict[str, Any] | None:
    """The frame's single active detail-resolution scope, if unambiguous."""

    scopes = [
        scope
        for scope in (frame.requirement_scopes or {}).values()
        if isinstance(scope, dict) and isinstance(scope.get("detail_resolution"), dict)
    ]
    if len(scopes) != 1:
        return None
    detail = scopes[0]["detail_resolution"]
    return detail if detail.get("status") == "active" else None


def _surface_total_records(frame: MaterializedFrame) -> int | None:
    """Known candidate cardinality from the visible list surface, if any."""

    totals = [
        surface.get("total_records")
        for surface in (frame.structured_surfaces or [])
        if isinstance(surface, dict)
    ]
    for total in totals:
        try:
            return max(0, int(total))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
    return None


def _open_candidate_step(
    frame: MaterializedFrame,
    detail: dict[str, Any],
    state: RecordWalkState,
    resolved: tuple[int, ...],
    candidate_records: int,
) -> RecordWalkStep | None:
    """Deterministically open the exact next unresolved candidate's row.

    The row is identified by its recorded id (``review_id``/``ID``/``id`` in
    the unresolved candidate's fields) against the leading token of the row
    group's checkbox label; the row's single visible action anchor is the tap
    target. Any ambiguity (no id, several matching groups, zero or several
    anchors) yields to the Worker.
    """

    candidate = detail.get("next_unresolved_candidate")
    if not isinstance(candidate, dict):
        return None
    fields = candidate.get("fields") or {}
    row_id = str(
        fields.get("review_id") or fields.get("ID") or fields.get("id") or ""
    ).strip()
    if not row_id:
        return None
    groups = {
        str(control.get("group_id"))
        for control in (frame.controls or [])
        if control.get("kind") == "checkbox_input"
        and control.get("group_id")
        and str(control.get("label") or "").strip().split(" ", 1)[0] == row_id
    }
    if len(groups) != 1:
        return None
    group = groups.pop()
    anchors = [
        control
        for control in (frame.controls or [])
        if str(control.get("group_id")) == group
        and control.get("kind") == "a"
        and control.get("in_viewport") is not False
        and positioned_rect(control) is not None
    ]
    if len(anchors) != 1:
        return None
    progress = progress_signature(frame)
    if (
        state.engaged
        and state.last_open_id == row_id
        and progress == state.last_progress
    ):
        return None
    state.last_open_id = row_id
    state.last_progress = progress
    state.engaged = True
    return RecordWalkStep(
        control=anchors[0],
        resolved=resolved,
        candidate_records=candidate_records,
        reason=(
            f"open next unresolved candidate ordinal {candidate.get('ordinal')} "
            f"(id {row_id}) via its row action"
        ),
    )


def record_walk_step(
    frame: MaterializedFrame,
    state: RecordWalkState,
) -> RecordWalkStep | None:
    """One deterministic walk step for this frame, or None to yield to policy.

    Two surfaces are driven: on a record editor (exactly one visible
    ``record_next``), advance the sibling-record chain; on the candidate list
    (an active assembly short of the surface's known total with exactly one
    visible ``page_next``), page the list so new candidates append. Anything
    ambiguous (zero or several scopes or controls) yields.
    """

    detail = walk_detail_scope(frame)
    if detail is None:
        state.reset()
        return None
    resolved = tuple(sorted(
        int(ordinal)
        for ordinal in (detail.get("resolved_candidate_ordinals") or [])
        if isinstance(ordinal, (int, float))
    ))
    try:
        candidate_records = max(0, int(detail.get("candidate_records") or 0))
    except (TypeError, ValueError):
        candidate_records = 0

    def _tappable(action: str) -> list[dict[str, Any]]:
        return [
            control
            for control in (frame.controls or [])
            if control.get("traversal_action") == action
            and control.get("in_viewport") is not False
            and positioned_rect(control) is not None
        ]

    record_next = _tappable("record_next")
    current_editor = detail.get("current_editor")
    freshly_resolved = bool(
        isinstance(current_editor, dict)
        and current_editor.get("resolved") is True
        and current_editor.get("pre_resolved") is not True
    )
    if not record_next and freshly_resolved:
        candidate = detail.get("next_unresolved_candidate")
        navigation_url = same_origin_navigation(
            frame,
            candidate.get("navigation_url") if isinstance(candidate, dict) else "",
        )
        if not navigation_url and detail.get("window_exhausted") is True:
            navigation_url = same_origin_navigation(
                frame, detail.get("candidate_source_url")
            )
        if navigation_url:
            state.engaged = True
            state.steps += 1
            return RecordWalkStep(
                control={},
                resolved=resolved,
                candidate_records=candidate_records,
                reason="open the next observed linked-detail target",
                navigation_url=navigation_url,
            )
    if not record_next:
        # Candidate-list surface. First: open the exact next unresolved
        # candidate when its row is visibly present — the Worker's most
        # expensive navigation (scroll-hunting a named row) is deterministic
        # here. Then: page the list while the assembly is short of the total.
        open_step = _open_candidate_step(frame, detail, state, resolved, candidate_records)
        if open_step is not None:
            return open_step
        total = _surface_total_records(frame)
        page_next = _tappable("page_next")
        page_previous = _tappable("page_previous")
        direction = None
        if detail.get("window_exhausted") is True and len(page_next) == 1:
            direction = page_next[0]
        elif len(page_next) == 1 and not page_previous:
            direction = page_next[0]
        elif len(page_previous) == 1 and not page_next:
            direction = page_previous[0]
        grew = not state.engaged or candidate_records != state.last_page_candidates
        if (
            total is not None
            and candidate_records < total
            and direction is not None
            and grew
        ):
            state.last_page_candidates = candidate_records
            state.engaged = True
            return RecordWalkStep(
                control=direction,
                resolved=resolved,
                candidate_records=candidate_records,
                reason=(
                    f"page candidate surface: {candidate_records}/{total} "
                    "candidates observed; advancing the list page"
                ),
            )
        state.reset()
        return None
    state.last_page_candidates = -1
    if len(record_next) != 1:
        state.reset()
        return None

    # Current-editor verdict, when the assembly provided one. The driver walks
    # strictly on fresh progress: only a record this frame just completed is a
    # green light to advance. An already-complete record, a gapped record, or
    # an unidentified editor all yield to the Worker immediately — pressing on
    # through them is how resolved prefixes get re-walked (live failure mode).
    # A missing key means the frame predates this signal: fall back to the
    # legacy stall budgets.
    if "current_editor" in detail:
        current_editor = detail.get("current_editor")
        if not (
            isinstance(current_editor, dict)
            and current_editor.get("resolved") is True
            and current_editor.get("pre_resolved") is not True
        ):
            return None

    progress = progress_signature(frame)
    if state.engaged and progress == state.last_progress:
        state.stalls += 1
    else:
        state.stalls = 0
    state.last_progress = progress
    if state.engaged:
        if resolved != state.last_resolved:
            state.no_credit_streak = 0
        else:
            state.no_credit_streak += 1
    state.last_resolved = resolved
    state.engaged = True

    step_cap = max(10, 2 * candidate_records + 4)
    if (
        state.stalls >= MAX_WALK_STALLS
        or state.no_credit_streak >= MAX_NO_CREDIT_STREAK
        or state.steps >= step_cap
    ):
        return None

    state.steps += 1
    return RecordWalkStep(
        control=record_next[0],
        resolved=resolved,
        candidate_records=candidate_records,
        reason=(
            f"record walk step {state.steps}: {len(resolved)}/{candidate_records} "
            "candidate(s) resolved; activating next-record traversal"
        ),
    )
