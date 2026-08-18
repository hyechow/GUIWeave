"""Private runtime data store.

LLMs receive only descriptors from this module.  Values are resolved exclusively
by deterministic runtime capabilities such as the Python transform.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Any

from jsonschema import validate

from gui_agent.core.tool_agent.contracts import CollectionRef, DataChunkRef, ResultRef


def _coverage_status(**evidence: Any) -> str:
    # Pure ReAct collectors: scope_status may never be "met" (vision rows, partial
    # predicate match). Do not hard-incomplete here; let row/end evidence decide.
    # Final worker complete() always forces status="complete" on the snapshot.
    scope_status = str(evidence.get("scope_status") or "met")
    row_count = evidence["row_count"]
    known_total = evidence.get("known_total")
    page_count = evidence.get("page_count")
    if evidence.get("totals_conflict") or (
        known_total is not None and row_count > known_total
    ):
        return "conflicting"
    if evidence["requested"] == "first_match" and row_count:
        return "complete"
    if evidence["structured"] and (
        evidence.get("all_pages")
        or page_count is None and (
            evidence.get("contiguous_to_end")
            or known_total is not None
            and row_count >= known_total
            and not evidence.get("partial")
            or evidence.get("surface_complete")
        )
    ):
        return "complete"
    if not evidence["structured"] and (
        page_count is None or evidence.get("all_pages")
    ) and (
        known_total is not None and row_count >= known_total
        or evidence.get("at_end")
        and known_total is None
        and evidence.get("start_seen") is not False
    ):
        return "complete"
    if (
        evidence.get("has_next_page") is True
        or page_count is not None and not evidence.get("all_pages")
        or known_total is not None and row_count < known_total
        or evidence.get("partial")
        or evidence.get("start_seen") is False
        and page_count is None
        and known_total is None
    ):
        return "incomplete"
    return "unknown"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _boundary_overlap(left: list[Any], right: list[Any]) -> int:
    """Longest exact suffix/prefix overlap between consecutive windows."""
    for size in range(min(len(left), len(right)), 0, -1):
        if left[-size:] == right[:size]:
            return size
    return 0


class RuntimeDataStore:
    def __init__(self) -> None:
        self._values: dict[str, Any] = {}
        self._chunks: dict[str, DataChunkRef] = {}
        self._collections: dict[str, CollectionRef] = {}
        self._results: dict[str, ResultRef] = {}
        self._data_results: set[str] = set()
        self._requirement_chunks: dict[tuple[str, str], list[str]] = defaultdict(list)
        self._dedupe: dict[tuple[str, str, str, str, str], str] = {}

    def put_chunk(
        self,
        *,
        requirement_id: str,
        frame_id: str,
        provider: str,
        rows: list[dict[str, Any]],
        row_schema: dict[str, Any],
        coverage: dict[str, Any],
    ) -> tuple[DataChunkRef, CollectionRef, bool]:
        for row in rows:
            validate(instance=row, schema=row_schema)
        digest = hashlib.sha256(_canonical(rows).encode()).hexdigest()[:16]
        collection_key = str(
            coverage.get("collection_key") or f"default:{requirement_id}"
        )
        bucket = (requirement_id, collection_key)
        window_key = str(coverage.get("window_key") or "")
        window_context = str(coverage.get("window_context") or "")
        window_state = "end" if coverage.get(
            "at_end", coverage.get("end_visible")
        ) else "partial"
        transport_key = window_key or f"{window_context}:{window_state}"
        key = (requirement_id, collection_key, provider, transport_key, digest)
        existing = self._dedupe.get(key)
        created = existing is None
        if existing is None:
            ref_id = f"chunk:{requirement_id}:{len(self._chunks) + 1}"
            chunk = DataChunkRef(
                ref=ref_id,
                requirement_id=requirement_id,
                frame_id=frame_id,
                provider=provider,
                row_count=len(rows),
                row_schema=row_schema,
                coverage=coverage,
            )
            self._dedupe[key] = ref_id
            self._chunks[ref_id] = chunk
            self._values[ref_id] = rows
            self._requirement_chunks[bucket].append(ref_id)
        else:
            chunk = self._chunks[existing]

        chunk_ids = list(self._requirement_chunks[bucket])
        collection_id = f"collection:{requirement_id}"
        chunk_coverage = [self._chunks[item].coverage for item in chunk_ids]
        coverage_samples = chunk_coverage if created else [*chunk_coverage, coverage]
        structured = any(
            item.get("source_scope") == "structured_surface" for item in coverage_samples
        )
        collection_rows: list[dict[str, Any]] = []
        previous_ref = ""
        for chunk_ref in chunk_ids:
            current_rows = self._values[chunk_ref]
            if previous_ref:
                previous = self._chunks[previous_ref]
                current = self._chunks[chunk_ref]
                previous_context = str(previous.coverage.get("window_context") or "")
                current_context = str(current.coverage.get("window_context") or "")
                if (
                    previous.provider == current.provider
                    and current.provider in {"vision", "structured"}
                    and previous_context
                    and previous_context == current_context
                    and previous.coverage.get("partial") is True
                ):
                    overlap = _boundary_overlap(self._values[previous_ref], current_rows)
                    if overlap:
                        current_rows = current_rows[overlap:]
                    elif reverse := _boundary_overlap(
                        current_rows, self._values[previous_ref]
                    ):
                        collection_rows[:0] = current_rows[:-reverse]
                        current_rows = []
            collection_rows.extend(current_rows)
            previous_ref = chunk_ref
        row_count = len(collection_rows)
        totals = {
            int(value)
            for item in coverage_samples
            if (value := item.get("total_records")) not in (None, "")
        }
        known_total = next(iter(totals)) if len(totals) == 1 else None
        pages_seen = sorted({
            int(value)
            for item in coverage_samples
            if (value := item.get("page_index")) not in (None, "")
        })
        page_counts = {
            int(value)
            for item in coverage_samples
            if (value := item.get("page_count")) not in (None, "")
        }
        page_count = next(iter(page_counts)) if len(page_counts) == 1 else None
        # Completion follows the latest observation even when its rows dedupe to
        # an earlier chunk (for example, revisiting the last page after backfill).
        last_coverage = coverage
        all_pages = bool(
            page_count is not None
            and pages_seen == list(range(1, page_count + 1))
        )
        at_end = bool(
            last_coverage.get("at_end", last_coverage.get("end_visible"))
        )
        start_values = [
            item.get("start_visible")
            for item in coverage_samples
            if isinstance(item.get("start_visible"), bool)
        ]
        start_seen = True if True in start_values else False if start_values else None
        scope_status = str(last_coverage.get("scope_status") or "met")
        contiguous_to_end = bool(
            at_end
            and page_count is None
            and pages_seen
            and pages_seen == list(range(1, max(pages_seen) + 1))
        )
        traversal_type = last_coverage.get("traversal_type")
        surface_complete = bool(
            (traversal_type == "static" or traversal_type == "scroll" and at_end)
            and not last_coverage.get("partial")
            and start_seen is not False
            and (known_total is None or row_count >= known_total)
        )
        totals_conflict = len(totals) > 1 or len(page_counts) > 1
        requested = str(last_coverage.get("requested") or "complete")
        coverage_status = _coverage_status(
            scope_status=scope_status,
            requested=requested,
            row_count=row_count,
            structured=structured,
            known_total=known_total,
            page_count=page_count,
            all_pages=all_pages,
            contiguous_to_end=contiguous_to_end,
            surface_complete=surface_complete,
            at_end=at_end,
            start_seen=start_seen,
            totals_conflict=totals_conflict,
            has_next_page=last_coverage.get("has_next_page"),
            partial=bool(last_coverage.get("partial")),
        )
        combined_coverage = {
            "requested": requested,
            "scope_status": scope_status,
            "requested_filters": dict(last_coverage.get("requested_filters") or {}),
            "applied_filters": dict(last_coverage.get("applied_filters") or {}),
            "collection_key": collection_key,
            "frames": len(chunk_ids),
            "status": coverage_status,
            "source_scope": (
                "structured_collection" if structured else "visual_collection"
            ),
            "known_total": known_total,
            "pages_seen": pages_seen,
            "page_count": page_count,
            "at_end": at_end,
            "movement": dict(last_coverage.get("movement") or {}),
            "may_contain_duplicates": len(chunk_ids) > 1 and (
                not structured
                or any(
                    item.get("traversal_type") == "scroll"
                    for item in chunk_coverage
                )
            ),
        }
        if start_seen is not None:
            combined_coverage["start_seen"] = start_seen
        for key in ("coverage_evidence", "empty_state_evidence"):
            if last_coverage.get(key) not in (None, ""):
                combined_coverage[key] = last_coverage[key]
        cardinality = "one" if last_coverage.get("cardinality") == "one" else "many"
        combined_coverage["cardinality"] = cardinality
        if scope_status == "met" and cardinality == "one":
            proves_multiple = row_count > 1 or any(
                isinstance(value, (int, float)) and value > 1
                for value in (known_total, page_count)
            ) or combined_coverage["movement"].get("has_next_page") is True
            if proves_multiple:
                combined_coverage["status"] = "conflicting"
            elif row_count == 1 and coverage_status != "conflicting":
                combined_coverage["status"] = "complete"
        collection = CollectionRef(
            ref=collection_id,
            requirement_id=requirement_id,
            chunk_refs=chunk_ids,
            row_count=row_count,
            row_schema=row_schema,
            coverage=combined_coverage,
        )
        self._collections[collection_id] = collection
        self._values[collection_id] = collection_rows
        return chunk, collection, created

    def collection_chunks(self, ref: str) -> list[list[dict[str, Any]]]:
        collection = self._collections.get(ref)
        if collection is None:
            raise KeyError(f"unknown CollectionRef {ref!r}")
        return [self._values[item] for item in collection.chunk_refs]

    def collection_rows(self, ref: str) -> list[dict[str, Any]]:
        self.collection_descriptor(ref)
        return list(self._values[ref])

    def collection_descriptor(self, ref: str) -> CollectionRef:
        try:
            return self._collections[ref]
        except KeyError as exc:
            raise KeyError(f"unknown CollectionRef {ref!r}") from exc

    def mark_scroll_end(self, ref: str) -> CollectionRef:
        """Record the terminal boundary proven by a downward scroll with no effect."""
        collection = self.collection_descriptor(ref)
        coverage = collection.coverage
        scrollable = coverage.get("source_scope") == "visual_collection" or (
            coverage.get("source_scope") == "structured_collection"
            and coverage.get("movement", {}).get("type") == "scroll"
        )
        if not scrollable:
            return collection
        started = coverage.get("start_seen") is True
        coverage = {
            **coverage,
            "at_end": True,
            "coverage_evidence": "downward_scroll_no_effect"
            + ("" if started else "_without_start"),
        }
        structured = coverage.get("source_scope") == "structured_collection"
        coverage["status"] = _coverage_status(
            scope_status=str(coverage.get("scope_status") or "met"),
            requested=str(coverage.get("requested") or "complete"),
            row_count=collection.row_count,
            structured=structured,
            known_total=coverage.get("known_total"),
            page_count=coverage.get("page_count"),
            all_pages=bool(
                coverage.get("page_count") is not None
                and coverage.get("pages_seen")
                == list(range(1, int(coverage["page_count"]) + 1))
            ),
            surface_complete=bool(started and structured),
            at_end=True,
            start_seen=coverage.get("start_seen"),
            has_next_page=False,
        )
        collection = collection.model_copy(update={"coverage": coverage})
        self._collections[ref] = collection
        return collection

    def collection_for_requirement(self, requirement_id: str) -> CollectionRef | None:
        """Return the latest accumulated collection for one logical requirement."""
        collection = self._collections.get(f"collection:{requirement_id}")
        if collection is None or collection.requirement_id != requirement_id:
            return None
        return collection

    def discard_requirement(self, requirement_id: str) -> None:
        """Drop stored chunks so a later assembly or nonempty query can publish."""
        collection_id = f"collection:{requirement_id}"
        self._collections.pop(collection_id, None)
        self._values.pop(collection_id, None)
        for bucket in [key for key in self._requirement_chunks if key[0] == requirement_id]:
            for chunk_id in self._requirement_chunks.pop(bucket):
                self._chunks.pop(chunk_id, None)
                self._values.pop(chunk_id, None)
        for key in [item for item in self._dedupe if item[0] == requirement_id]:
            del self._dedupe[key]

    def restore_collection(self, descriptor: CollectionRef, rows: list[dict[str, Any]]) -> None:
        """Restore one recorded GUI artifact without replaying browser observations."""
        for row in rows:
            validate(instance=row, schema=descriptor.row_schema)
        if len(rows) != descriptor.row_count:
            raise ValueError(
                f"recorded CollectionRef {descriptor.ref!r} declares "
                f"{descriptor.row_count} rows but contains {len(rows)}"
            )
        self._collections[descriptor.ref] = descriptor
        self._values[descriptor.ref] = list(rows)

    def ref_row_schema(self, ref: str) -> dict[str, Any] | None:
        """Return the normalized row schema for a collection-like input ref."""
        collection = self._collections.get(ref)
        return collection.row_schema if collection is not None else None

    def put_result(
        self,
        value: Any,
        schema: dict[str, Any],
        *,
        summary: str = "",
        source_refs: list[str] | None = None,
    ) -> ResultRef:
        validate(instance=value, schema=schema)
        ref_id = f"result:{len(self._results) + 1}"
        descriptor = ResultRef(ref=ref_id, value_schema=schema, summary=summary)
        self._results[ref_id] = descriptor
        self._values[ref_id] = value
        if any(
            ref in self._collections or ref in self._data_results
            for ref in source_refs or []
        ):
            self._data_results.add(ref_id)
        return descriptor

    def is_data_result(self, ref: str) -> bool:
        return ref in self._data_results

    def result_descriptor(self, ref: str) -> ResultRef:
        try:
            return self._results[ref]
        except KeyError as exc:
            raise KeyError(f"unknown ResultRef {ref!r}") from exc

    def result_value(self, ref: str) -> Any:
        self.result_descriptor(ref)
        return self._values[ref]

    def resolve_value(self, ref: str) -> Any:
        """Resolve any runtime-owned data reference for deterministic Workers.

        This method is intentionally never exposed to an LLM-facing contract.
        The Coding Master can route ref strings, while only runtime capabilities
        are allowed to dereference their private values.
        """
        if ref not in self._values:
            raise KeyError(f"unknown runtime data ref {ref!r}")
        return self._values[ref]

    def private_dump(self) -> dict[str, Any]:
        """Debug artifact for the local run directory; never pass this to an LLM."""
        return {
            "chunks": {key: value.model_dump(mode="json") for key, value in self._chunks.items()},
            "collections": {
                key: value.model_dump(mode="json") for key, value in self._collections.items()
            },
            "results": {key: value.model_dump(mode="json") for key, value in self._results.items()},
            "values": self._values,
        }
