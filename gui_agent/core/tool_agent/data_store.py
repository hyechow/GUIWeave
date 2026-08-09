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


class RuntimeDataStore:
    def __init__(self) -> None:
        self._values: dict[str, Any] = {}
        self._chunks: dict[str, DataChunkRef] = {}
        self._collections: dict[str, CollectionRef] = {}
        self._results: dict[str, ResultRef] = {}
        self._requirement_chunks: dict[str, list[str]] = defaultdict(list)
        self._dedupe: dict[tuple[str, str], str] = {}

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
        key = (requirement_id, digest)
        existing = self._dedupe.get(key)
        created = existing is None
        if existing is None:
            ref_id = f"chunk:{requirement_id}:{len(self._requirement_chunks[requirement_id]) + 1}"
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
            self._requirement_chunks[requirement_id].append(ref_id)
        else:
            chunk = self._chunks[existing]

        chunk_ids = list(self._requirement_chunks[requirement_id])
        collection_id = f"collection:{requirement_id}"
        row_count = len(self._unique_rows(chunk_ids))
        combined_coverage = {
            "requested": "complete",
            "frames": len(chunk_ids),
            "end_visible": any(
                bool(self._chunks[item].coverage.get("end_visible")) for item in chunk_ids
            ),
        }
        collection = CollectionRef(
            ref=collection_id,
            requirement_id=requirement_id,
            chunk_refs=chunk_ids,
            row_count=row_count,
            row_schema=row_schema,
            coverage=combined_coverage,
        )
        self._collections[collection_id] = collection
        return chunk, collection, created

    def collection_chunks(self, ref: str) -> list[list[dict[str, Any]]]:
        collection = self._collections.get(ref)
        if collection is None:
            raise KeyError(f"unknown CollectionRef {ref!r}")
        return [self._values[item] for item in collection.chunk_refs]

    def collection_rows(self, ref: str) -> list[dict[str, Any]]:
        collection = self._collections.get(ref)
        if collection is None:
            raise KeyError(f"unknown CollectionRef {ref!r}")
        return self._unique_rows(collection.chunk_refs)

    def _unique_rows(self, chunk_refs: list[str]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for chunk_ref in chunk_refs:
            for row in self._values[chunk_ref]:
                identity = _canonical(row)
                if identity in seen:
                    continue
                seen.add(identity)
                rows.append(row)
        return rows

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
