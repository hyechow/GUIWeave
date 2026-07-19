"""Platform-neutral, read-only projection of one Observation.

This module owns no state and performs no LLM calls.  DOM/AX/table sensors are
optional enrichments; the screenshot remains the cross-platform fallback.
"""

from __future__ import annotations

from hashlib import sha1
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from gui_agent.core.schemas import Observation


class NormalizedDataset(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ref: str
    source: Literal["table", "semantic", "visual"]
    fields: list[str] = Field(default_factory=list)
    records: list[dict[str, JsonValue]] = Field(default_factory=list)
    caption: str = ""
    total: int | None = None
    traversal: dict[str, JsonValue] | None = None
    surface_fingerprint: str
    region: str = ""
    structured: bool = False
    partial: bool = False
    provenance_incomplete: bool = False

    @property
    def reliable(self) -> bool:
        return bool(
            self.structured
            and (
                self.total is not None
                or isinstance(self.traversal, dict)
                and self.traversal.get("type") in {"paged", "scroll"}
            )
        )

    def as_table(self) -> dict[str, Any]:
        """Compatibility-free transport shape consumed by CollectionSlice projection."""
        return {
            "path": self.region,
            "caption": self.caption,
            "headers": list(self.fields),
            "rows": [dict(record) for record in self.records],
            "total_records": self.total,
            "traversal": dict(self.traversal or {}),
            "partial": self.partial,
            "_source": self.source,
            "_surface_fingerprint": self.surface_fingerprint,
            "_provenance_incomplete": self.provenance_incomplete,
        }

    def as_context(self) -> dict[str, JsonValue]:
        return {
            "ref": self.ref,
            "source": self.source,
            "fields": list(self.fields),
            "record_count": len(self.records),
            # Full records stay available to the deterministic kernel through ``as_table``.
            # The LLM only needs schema plus a small exemplar to choose a projection.
            "sample_records": [dict(record) for record in self.records[:3]],
            "caption": self.caption,
            "total": self.total,
            "traversal": dict(self.traversal or {}),
            "partial": self.partial,
            "provenance_incomplete": self.provenance_incomplete,
        }


class NormalizedObservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    page: dict[str, JsonValue] = Field(default_factory=dict)
    controls: list[dict[str, JsonValue]] = Field(default_factory=list)
    semantic: list[dict[str, JsonValue]] = Field(default_factory=list)
    datasets: list[NormalizedDataset] = Field(default_factory=list)
    applied_filters: dict[str, JsonValue] = Field(default_factory=dict)
    visual_available: bool = False

    def source(self, name: str) -> JsonValue:
        values: dict[str, JsonValue] = {
            "page": self.page,
            "controls": self.controls,
            "semantic": self.semantic,
            "datasets": [dataset.as_table() for dataset in self.datasets],
        }
        return values[name]

    def as_context(self) -> dict[str, JsonValue]:
        return {
            "page": self.page,
            "controls": self.controls,
            "semantic": self.semantic,
            "datasets": [dataset.as_context() for dataset in self.datasets],
            "applied_filters": self.applied_filters,
            "visual_available": self.visual_available,
        }


def _json_value(value: Any) -> JsonValue:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return str(value)


def _surface_fingerprint(raw: dict[str, Any], index: int) -> str:
    path = str(raw.get("path") or "").strip()
    caption = str(raw.get("caption") or "").strip()
    headers = [str(header) for header in raw.get("headers") or []]
    identity = path or caption or json.dumps(headers, ensure_ascii=False)
    return f"table:{identity or index}"


def materialize_observation(observation: Observation | None) -> NormalizedObservation:
    if observation is None:
        return NormalizedObservation()
    datasets: list[NormalizedDataset] = []
    for index, raw in enumerate(getattr(observation, "tables", None) or []):
        if not isinstance(raw, dict):
            continue
        rows = [
            {str(key): _json_value(value) for key, value in row.items()}
            for row in raw.get("rows") or []
            if isinstance(row, dict)
        ]
        fields = [str(header) for header in raw.get("headers") or []]
        if not fields:
            fields = list(dict.fromkeys(key for row in rows for key in row))
        total_raw = raw.get("total_records")
        try:
            total = int(total_raw) if total_raw not in (None, "") else None
        except (TypeError, ValueError):
            total = None
        datasets.append(NormalizedDataset(
            ref=f"table:{index}",
            source="table",
            fields=fields,
            records=rows,
            caption=str(raw.get("caption") or ""),
            total=total,
            traversal=_json_value(raw.get("traversal")) if raw.get("traversal") else None,
            surface_fingerprint=_surface_fingerprint(raw, index),
            region=str(raw.get("path") or ""),
            structured=True,
            partial=bool(raw.get("partial")),
            provenance_incomplete=not bool(raw.get("path") and fields),
        ))
    semantic: list[dict[str, JsonValue]] = []
    for raw in getattr(observation, "semantic_tree", None) or []:
        if not isinstance(raw, dict):
            continue
        item = {
            key: _json_value(raw.get(key))
            for key in ("role", "key", "value", "url", "ref", "depth", "in_viewport")
            if raw.get(key) not in (None, "")
        }
        if item:
            semantic.append(item)
    controls = [
        {str(key): _json_value(value) for key, value in raw.items()}
        for raw in getattr(observation, "form_controls", None) or []
        if isinstance(raw, dict)
    ]
    return NormalizedObservation(
        page={
            "url": str(getattr(observation, "url", None) or ""),
            "title": str(getattr(observation, "title", None) or ""),
            "source": str(getattr(observation, "source", None) or ""),
        },
        controls=controls,
        semantic=semantic,
        datasets=datasets,
        applied_filters={
            str(key): _json_value(value)
            for key, value in (getattr(observation, "applied_filters", None) or {}).items()
        },
        visual_available=bool(getattr(observation, "png_bytes", b"")),
    )


def visual_dataset(
    observation: Observation,
    *,
    fields: list[str],
    records: list[dict[str, Any]],
) -> NormalizedDataset:
    source = str(observation.source or "visual")
    fingerprint = sha1(
        json.dumps([source, fields], ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    traversal = observation.viewport if isinstance(observation.viewport, dict) else None
    return NormalizedDataset(
        ref="visual:main",
        source="visual",
        fields=list(fields),
        records=[
            {str(key): _json_value(value) for key, value in record.items()}
            for record in records
        ],
        caption="current visual collection",
        traversal=_json_value(traversal) if traversal else None,
        surface_fingerprint=f"visual:{source}:{fingerprint}",
        region="body" if source == "browser" else "main",
        structured=False,
        provenance_incomplete=True,
    )


__all__ = [
    "NormalizedDataset",
    "NormalizedObservation",
    "materialize_observation",
    "visual_dataset",
]
