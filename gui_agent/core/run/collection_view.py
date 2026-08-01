"""CollectionView — read-only cross-slice collection projection from EventJournal.

- EventJournal: fact authority. Independent ``CollectionSliceEvent`` values carry each
  observed slice's normalized records, provenance and boundary evidence.
- CollectionView: a PURE projection of those frames (this module).
- Acquire: the sole consumer of this view while collecting one bound collection.
- StatementOutcome.outputs: the only channel through which materialized records enter the
  Program environment.

CollectionView is NOT a state machine. It MUST NOT expose:

    advance(), next_action(), should_continue(), is_complete(), phase

It only describes facts; Acquire combines it with adapter capabilities and acquisition-only
policy proposals.
"""

from __future__ import annotations

import re
import json
from dataclasses import dataclass
from hashlib import sha1
from typing import Any, Literal
from urllib.parse import urlsplit

from gui_agent.core.schemas import (
    CollectionBoundary,
    CollectionProvenance,
    CollectionSliceEvent,
    JournalEvent,
    Observation,
    StatementContract,
)
from gui_agent.core.run.observation_materializer import (
    NormalizedDataset,
    materialize_observation,
)

# Per-frame transport budget. Frames exceeding this set are marked truncated and keep the
# first MAX_FRAME_RECORDS records; semantic filtering remains a compiled Compute step.
MAX_FRAME_RECORDS = 200

BoundaryKind = Literal["known_total", "at_end", "has_next_page", "not_at_end", "unknown"]
MoveResult = Literal["unknown", "new_content", "no_change", "moved_boundary"]
CoverageState = Literal["unknown", "complete", "incomplete", "conflicting"]


# --- frozen projection types -------------------------------------------------


@dataclass(frozen=True)
class CollectionSegment:
    """One frame's contribution to the collection, cited by its journal event_ref."""

    event_ref: str
    record_count: int
    source: str
    collection_key: str


@dataclass(frozen=True)
class BoundaryEvidence:
    """One frame's boundary claim; a terminal proposal may cite it via event_ref."""

    event_ref: str
    kind: BoundaryKind
    claim: str


@dataclass(frozen=True)
class MovementCapability:
    """Current-frame movement affordance (last frame only). Descriptive, not a directive."""

    kind: Literal["scroll", "paged", "visual"]
    can_forward: bool
    can_backward: bool
    collection_key: str


@dataclass(frozen=True)
class CollectionView:
    """Frozen cross-frame projection of collected records and coverage evidence.

    A pure projection, not a state owner. Exposes no advance / next_action /
    should_continue / is_complete / phase surface. Acquire reads it; nothing mutates it.

    Only the latest collection provenance is materialized. Earlier collection keys remain
    visible as drift diagnostics but their records are never silently mixed into the active
    collection.
    """

    instance_id: str
    collection_key: str
    collection_keys: tuple[str, ...]
    records: tuple[dict[str, Any], ...]
    seen_slice_keys: frozenset[str]
    observed_segments: tuple[CollectionSegment, ...]
    known_total: int | None
    boundary_evidence: tuple[BoundaryEvidence, ...]
    available_movements: tuple[MovementCapability, ...]
    last_move_result: MoveResult
    provenance_drift: bool
    provenance_incomplete: bool
    total_drift: bool
    truncated: bool
    may_contain_duplicates: bool


# --- pure record-extraction helpers (copied from list_runtime, decoupled) ----
#
# Identity policy (design acceptance: 「采集层不根据业务字段或显示文本做语义去重」):
# the collection layer performs NO business-key dedup. Cross-frame dedup is slice-level — a
# frame already absorbed (same surface_id + content_key) is not re-counted. Within a frame,
# rows carry a positional id ``row:<ordinal>``. Overlapping windows are allowed to repeat;
# business dedup (same name different record, etc.) is left to a compiled Compute step.


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def _slice_key(event: CollectionSliceEvent) -> str:
    """Certified transport identity; missing window identity means append-all."""
    if event.window_key and event.content_key:
        return f"{event.collection_key}|{event.window_key}|{event.content_key}"
    return event.event_ref


def _table_content_key(table: dict[str, Any]) -> str:
    """sha1 over the frame's sorted row content — pure content-change measurement."""
    rows = table.get("rows") or []
    text = repr(
        [
            sorted((str(key), str(value or "")) for key, value in row.items())
            for row in rows
            if isinstance(row, dict)
        ]
    )
    return sha1(text.encode("utf-8")).hexdigest()


def _table_surface_id(table: dict[str, Any], fallback: str) -> str:
    explicit = str(table.get("_surface_fingerprint") or "").strip()
    if explicit:
        return explicit
    path = str(table.get("path") or "").strip()
    caption = _norm(table.get("caption"))
    headers = ",".join(_norm(header) for header in (table.get("headers") or []))
    return f"table:{path or caption or headers or fallback}"


def _normalized_route(url: str) -> str:
    parsed = urlsplit(str(url or ""))
    return parsed.path.rstrip("/") or "/"


def _window_key(table: dict[str, Any]) -> str:
    traversal = table.get("traversal")
    if not isinstance(traversal, dict):
        return ""
    if traversal.get("type") != "paged":
        return ""
    page_index = traversal.get("page_index")
    return f"page:{page_index}" if page_index not in (None, "") else ""


def _provenance(
    observation: Observation,
    table: dict[str, Any],
    contract: StatementContract,
) -> CollectionProvenance:
    headers = [str(header) for header in (table.get("headers") or [])]
    surface = _table_surface_id(table, contract.id)
    structural_surface = str(table.get("path") or "").strip()
    route = _normalized_route(getattr(observation, "url", "") or "")
    filters = {
        str(key): value
        for key, value in (getattr(observation, "applied_filters", None) or {}).items()
    }
    schema = sha1(
        json.dumps([_norm(header) for header in headers], ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return CollectionProvenance(
        surface_fingerprint=surface,
        filter_snapshot=filters,
        schema_fingerprint=schema,
        route=route,
        incomplete=bool(table.get("_provenance_incomplete")) or not bool(
            structural_surface and schema
        ),
    )


def _collection_key(provenance: CollectionProvenance) -> str:
    payload = provenance.model_dump(mode="json", exclude={"incomplete"})
    return sha1(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def collection_candidates(
    observation: Observation,
    *,
    extra_datasets: list[NormalizedDataset] | None = None,
) -> list[dict[str, Any]]:
    """Return normalized cross-platform collection candidates without choosing one."""
    candidates: list[dict[str, Any]] = []
    normalized = materialize_observation(observation)
    datasets = [*normalized.datasets, *(extra_datasets or [])]
    for dataset in datasets:
        table = dataset.as_table()
        rows = [row for row in dataset.records if isinstance(row, dict)]
        headers = list(dataset.fields)
        if not headers:
            continue
        candidates.append(
            {
                "ref": dataset.ref,
                "table": table,
                "surface_fingerprint": dataset.surface_fingerprint,
                "reliable": dataset.reliable,
                "caption": dataset.caption,
                "headers": headers,
                "record_count": len(rows),
            }
        )
    for region in getattr(observation, "collection_regions", None) or []:
        cells = [cell.model_dump(mode="json") for cell in region.cells]
        candidates.append(
            {
                "ref": region.ref,
                "table": {
                    "_surface_fingerprint": region.surface_fingerprint,
                    "_region_bounds": list(region.bounds) if region.bounds else None,
                    "_collection_cells": cells,
                    "traversal": dict(region.traversal),
                },
                "surface_fingerprint": region.surface_fingerprint,
                "reliable": False,
                "projection": "cells",
                "caption": region.caption,
                "headers": [],
                "record_count": None,
            }
        )
    return candidates


def _known_total_from_table(table: dict[str, Any]) -> int | None:
    raw = table.get("total_records")
    if raw in (None, ""):
        return None
    try:
        total = int(raw)
    except (TypeError, ValueError):
        return None
    return total if total >= 0 else None


def _boundary_from_traversal(signal: Any) -> CollectionBoundary:
    """Pure translation of an adapter traversal signal into a boundary fact.

    Drops the can_forward/can_backward accumulation that drove the retired TraversalSession
    state machine; only the boundary half is a fact (the movement half is descriptive and
    lives in MovementCapability).
    """
    if not isinstance(signal, dict):
        return "unknown"
    kind = signal.get("type")
    if kind == "paged":
        has_next = signal.get("has_next_page")
        if has_next is True:
            return "has_next_page"
        if has_next is False:
            return "at_end"
        return "unknown"
    if kind == "scroll":
        at_end = signal.get("at_scroll_end")
        if at_end is True:
            return "at_end"
        can_more = signal.get("can_scroll_more")
        if can_more is True:
            return "has_next_page"
        if can_more is False:
            return "not_at_end"
        return "unknown"
    if kind == "static":
        return "at_end"
    return "unknown"


def _movement_from_signal(signal: Any, collection_key: str) -> MovementCapability | None:
    """Descriptive current-frame movement affordance (never a directive)."""
    if not isinstance(signal, dict):
        return None
    kind = signal.get("type")
    if kind == "paged":
        return MovementCapability(
            kind="paged",
            can_forward=signal.get("has_next_page") is True,
            can_backward=signal.get("has_prev_page") is True,
            collection_key=collection_key,
        )
    if kind == "scroll":
        return MovementCapability(
            kind="scroll",
            can_forward=signal.get("can_scroll_more") is True,
            can_backward=signal.get("can_scroll_back") is True,
            collection_key=collection_key,
        )
    return None


# --- frame sensor: observation -> CollectionSliceEvent -----------------------


def _contract_collects_records(contract: StatementContract) -> bool:
    return any(spec.type == "list[record]" for spec in contract.returns.values())


def project_collection_slice(
    observation: Observation,
    contract: StatementContract,
    *,
    instance_id: str,
    after_turn: int,
    event_ref: str,
    frame_ref: str,
    table: dict[str, Any] | None = None,
    strategy: Literal["structured", "react"] = "structured",
) -> CollectionSliceEvent | None:
    """Project one explicitly bound table into an independent Journal slice.

    Returns None unless the contract declares a ``list[record]`` return and a backing
    table resolves with rows. Pure sensor: reads ``observation.tables``/``viewport``, emits
    facts, decides nothing. Visual-only platforms (no DOM table) return None here; the
    mobile vision sensor is a follow-up adapter task.
    """
    if not _contract_collects_records(contract):
        return None
    if table is None:
        reliable = [item for item in collection_candidates(observation) if item["reliable"]]
        table = reliable[0]["table"] if len(reliable) == 1 else None
    if table is None:
        return None
    headers = [str(h) for h in (table.get("headers") or [])]
    raw_rows = [r for r in (table.get("rows") or []) if isinstance(r, dict)]
    if not headers:
        return None
    provenance = _provenance(observation, table, contract)
    records: list[dict[str, Any]] = []
    for raw in raw_rows:
        values = {str(key): str(value or "").strip() for key, value in raw.items()}
        if not any(values.values()):
            continue
        records.append(values)
    truncated = len(records) > MAX_FRAME_RECORDS
    return CollectionSliceEvent(
        event_ref=event_ref,
        after_turn=after_turn,
        statement_instance_id=instance_id,
        statement_id=contract.id,
        frame_ref=frame_ref,
        collection_key=_collection_key(provenance),
        provenance=provenance,
        window_key=_window_key(table),
        content_key=_table_content_key(table),
        records=records[:MAX_FRAME_RECORDS],
        known_total=_known_total_from_table(table),
        boundary=_boundary_from_traversal(table.get("traversal")),
        source=str(table.get("_source") or "table"),  # type: ignore[arg-type]
        strategy=strategy,
        truncated=truncated,
    )


# --- reducer: journal turns -> CollectionView --------------------------------


def _boundary_evidence_for(event: CollectionSliceEvent) -> BoundaryEvidence:
    kind: BoundaryKind
    claim: str
    if event.boundary == "at_end":
        kind = "at_end"
        claim = "frame reached the collection forward boundary"
    elif event.boundary == "has_next_page":
        kind = "has_next_page"
        claim = "frame shows a further page/window is available"
    elif event.boundary == "not_at_end":
        kind = "not_at_end"
        claim = "frame is not yet at a boundary"
    else:
        kind = "unknown"
        claim = "no boundary signal observed"
    return BoundaryEvidence(event_ref=event.event_ref, kind=kind, claim=claim)


def _classify_move_result(content_keys: list[str]) -> MoveResult:
    if len(content_keys) < 2:
        return "unknown"
    prev, cur = content_keys[-2], content_keys[-1]
    if not prev or not cur:
        return "unknown"
    return "no_change" if cur == prev else "new_content"


def build_collection_view(
    *,
    instance_id: str,
    contract: StatementContract,
    history: list[JournalEvent],
    current_observation: Observation | None = None,
) -> CollectionView:
    """Project independent Journal slice events into a frozen CollectionView.

    The current observation is never folded in: callers must append its slice event before
    evaluating coverage or materializing terminal outputs. That ordering is what makes live,
    checkpoint and replay projections identical.
    """
    del current_observation
    scoped = [
        event for event in history
        if isinstance(event, CollectionSliceEvent)
        and event.statement_instance_id == instance_id
        and event.statement_id == contract.id
    ]
    collection_keys = tuple(dict.fromkeys(event.collection_key for event in scoped))
    active_key = scoped[-1].collection_key if scoped else ""
    active = [event for event in scoped if event.collection_key == active_key]

    records: list[dict[str, Any]] = []
    seen_slices: set[str] = set()
    segments: list[CollectionSegment] = []
    boundaries: list[BoundaryEvidence] = []
    known_total: int | None = None
    known_totals: set[int] = set()
    content_keys: list[str] = []
    last_event: CollectionSliceEvent | None = None
    truncated = False
    may_contain_duplicates = False

    for event in active:
        slice_key = _slice_key(event)
        content_keys.append(event.content_key)
        if event.known_total is not None:
            known_total = event.known_total
            known_totals.add(event.known_total)
        boundaries.append(_boundary_evidence_for(event))
        last_event = event
        truncated = truncated or event.truncated
        may_contain_duplicates = may_contain_duplicates or not bool(event.window_key)
        if slice_key in seen_slices:
            continue
        seen_slices.add(slice_key)
        records.extend(event.records)
        segments.append(
            CollectionSegment(
                event_ref=event.event_ref,
                record_count=len(event.records),
                source=event.source,
                collection_key=event.collection_key,
            )
        )

    available_movements: tuple[MovementCapability, ...] = ()
    if last_event is not None:
        movement = _movement_from_signal(
            _traversal_signal_for(last_event), last_event.collection_key
        )
        if movement is not None:
            available_movements = (movement,)

    return CollectionView(
        instance_id=instance_id,
        collection_key=active_key,
        collection_keys=collection_keys,
        records=tuple(records),
        seen_slice_keys=frozenset(seen_slices),
        observed_segments=tuple(segments),
        known_total=known_total,
        boundary_evidence=tuple(boundaries),
        available_movements=available_movements,
        last_move_result=_classify_move_result(content_keys),
        provenance_drift=len(collection_keys) > 1,
        provenance_incomplete=(
            last_event.provenance.incomplete if last_event is not None else True
        ),
        total_drift=len(known_totals) > 1,
        truncated=truncated,
        may_contain_duplicates=may_contain_duplicates,
    )


def _traversal_signal_for(event: CollectionSliceEvent) -> dict[str, Any] | None:
    """Recover a minimal traversal signal from a persisted frame for movement description.

    Boundary is the authoritative fact (persisted on the frame); movement capability is
    descriptive only. We reconstruct the forward affordance from the boundary kind so the
    current-frame movement hint stays consistent with the boundary evidence.
    """
    kind = "paged" if event.window_key.startswith("page:") else "scroll"
    if event.boundary == "has_next_page":
        return {"type": kind, "has_next_page": True, "can_scroll_more": True}
    if event.boundary == "at_end":
        return {
            "type": kind,
            "has_next_page": False,
            "can_scroll_more": False,
            "at_scroll_end": True,
        }
    if event.boundary == "not_at_end":
        return {"type": kind, "can_scroll_more": True}
    return None


def _classify_coverage(
    collected: int,
    total: int | None,
    last_kind: str,
) -> CoverageState:
    """Mechanical coverage predicate over record count, authoritative total and current boundary.

    Never decides an action. The current frontier (last boundary) decides whether a next page
    still exists; earlier frames' ``has_next_page`` is historical (page 1 having a next page does
    not contradict the final page reaching ``at_end``). The only genuine conflict is the record
    count contradicting an authoritative known total (design: 「已有明确『仍存在下一页』的证据时，
    不允许声称完整」).
    """
    has_next_now = last_kind == "has_next_page"
    at_end_now = last_kind == "at_end"
    if total == 0 and collected == 0 and not has_next_now:
        return "complete"
    if total is not None and total > 0 and collected > total:
        return "conflicting"
    if has_next_now:
        return "incomplete"
    if total is not None and total > 0 and collected >= total:
        return "complete"
    if total is not None and collected < total:
        return "incomplete"
    if at_end_now:
        return "complete"
    return "unknown"


def coverage_status(view: CollectionView) -> CoverageState:
    """Classify the view's intrinsic coverage state from its facts alone."""
    if view.truncated or view.total_drift:
        return "conflicting"
    if view.known_total is not None and any(
        segment.record_count > view.known_total
        for segment in view.observed_segments
    ):
        return "conflicting"
    last_kind = view.boundary_evidence[-1].kind if view.boundary_evidence else "unknown"
    if view.may_contain_duplicates and view.known_total is not None:
        if last_kind == "has_next_page":
            return "incomplete"
        if len(view.records) < view.known_total:
            return "incomplete"
        if last_kind == "at_end":
            return "complete"
        return "unknown"
    return _classify_coverage(len(view.records), view.known_total, last_kind)


def materialize_collection(
    *,
    instance_id: str,
    contract: StatementContract,
    history: list[JournalEvent],
) -> tuple[list[dict[str, Any]], CoverageState]:
    """Materialize records solely from the append-only Journal projection."""
    view = build_collection_view(
        instance_id=instance_id, contract=contract, history=history,
    )
    return list(view.records), coverage_status(view)


__all__ = [
    "MAX_FRAME_RECORDS",
    "BoundaryEvidence",
    "BoundaryKind",
    "CollectionProvenance",
    "CollectionSliceEvent",
    "CollectionSegment",
    "CollectionView",
    "CoverageState",
    "MovementCapability",
    "MoveResult",
    "build_collection_view",
    "collection_candidates",
    "coverage_status",
    "materialize_collection",
    "project_collection_slice",
]
