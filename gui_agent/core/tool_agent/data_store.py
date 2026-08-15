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


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _boundary_overlap(left: list[Any], right: list[Any]) -> int:
    """Longest exact suffix/prefix overlap between consecutive visual windows."""
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
            overlap = 0
            if previous_ref:
                previous = self._chunks[previous_ref]
                current = self._chunks[chunk_ref]
                previous_context = str(previous.coverage.get("window_context") or "")
                current_context = str(current.coverage.get("window_context") or "")
                if (
                    previous.provider == current.provider == "vision"
                    and previous_context
                    and previous_context == current_context
                    and previous.coverage.get("partial") is True
                ):
                    overlap = _boundary_overlap(self._values[previous_ref], current_rows)
            collection_rows.extend(current_rows[overlap:])
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
            and (known_total is None or row_count >= known_total)
        )
        totals_conflict = len(totals) > 1 or len(page_counts) > 1
        requested = str(last_coverage.get("requested") or "complete")
        if scope_status != "met":
            coverage_status = "incomplete"
        elif totals_conflict or (
            known_total is not None and row_count > known_total
        ):
            coverage_status = "conflicting"
        elif requested == "first_match" and row_count:
            coverage_status = "complete"
        elif structured and (
            all_pages
            or page_count is None and (
                contiguous_to_end
                or known_total is not None
                and row_count >= known_total
                and not last_coverage.get("partial")
                or surface_complete
            )
        ):
            coverage_status = "complete"
        elif not structured and at_end and (
            page_count is None or all_pages
        ) and (
            known_total is None or row_count >= known_total
        ):
            coverage_status = "complete"
        elif (
            last_coverage.get("has_next_page") is True
            or (page_count is not None and not all_pages)
            or (known_total is not None and row_count < known_total)
            or last_coverage.get("partial")
        ):
            coverage_status = "incomplete"
        else:
            coverage_status = "unknown"
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
        for key in ("coverage_evidence", "empty_state_evidence"):
            if last_coverage.get(key) not in (None, ""):
                combined_coverage[key] = last_coverage[key]
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

    def collection_for_requirement(self, requirement_id: str) -> CollectionRef | None:
        """Return the latest accumulated collection for one logical requirement."""
        collection = self._collections.get(f"collection:{requirement_id}")
        if collection is None or collection.requirement_id != requirement_id:
            return None
        return collection

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

    def put_result(self, value: Any, schema: dict[str, Any], *, summary: str = "") -> ResultRef:
        validate(instance=value, schema=schema)
        ref_id = f"result:{len(self._results) + 1}"
        descriptor = ResultRef(ref=ref_id, value_schema=schema, summary=summary)
        self._results[ref_id] = descriptor
        self._values[ref_id] = value
        return descriptor

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
