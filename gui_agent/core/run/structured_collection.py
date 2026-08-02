"""Lossless projection of ordered platform cells into collection records."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from gui_agent.core.config import resolve_llm_config
from gui_agent.prompts import load_prompt_text
from llm.provider_config import dashscope_extra_body
from llm.structured import invoke_structured


_SYSTEM = load_prompt_text("task.statement.collection_projection")


class _FieldSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    source_ref: str


class _ProjectedRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record: str
    fields: list[_FieldSource]


class _Projection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    anchor_cell: str = ""
    records: list[_ProjectedRecord] = Field(default_factory=list)


ProjectionCall = Callable[[dict[str, Any], type[BaseModel]], BaseModel]


def _richness(cell: dict[str, Any]) -> tuple[int, int, int]:
    return (
        int(not cell.get("clipped_top") and not cell.get("clipped_bottom")),
        len(" ".join(str(value) for value in cell.get("texts") or [])),
        len(cell.get("controls") or []),
    )


@dataclass
class CellStream:
    """Join forward cell windows through exact continuous overlap."""

    _cells: list[dict[str, Any]] = field(default_factory=list)
    _window_offset: int = 0

    def add(self, window: Sequence[dict[str, Any]]) -> None:
        current = [dict(cell) for cell in window]
        if not self._cells:
            self._cells = current
            return
        alignment = self._longest_overlap(current)
        if alignment is None:
            raise ValueError("collection windows do not have one exact continuous overlap")
        offset, overlap_start = alignment
        for index, cell in enumerate(current):
            position = offset + index
            if position == len(self._cells):
                self._cells.append(cell)
                continue
            if position > len(self._cells):
                raise ValueError("collection windows leave an unobserved gap")
            existing = self._cells[position]
            if existing.get("content_key") != cell.get("content_key"):
                incomplete = any(
                    item.get("clipped_top") or item.get("clipped_bottom")
                    for item in (existing, cell)
                )
                if incomplete:
                    # Viewport-edge cells are explicitly partial sensor readings.
                    # They may expose disjoint fragments of one item across frames;
                    # only a complete/complete disagreement is a real conflict.
                    if _richness(cell) > _richness(existing):
                        self._cells[position] = cell
                    continue
                old_texts = set(existing.get("texts") or [])
                new_texts = set(cell.get("texts") or [])
                if position < overlap_start:
                    # A sensor may retain stale or clipped cells before the exact
                    # forward overlap. They cannot revise an established prefix.
                    continue
                if not (old_texts <= new_texts or new_texts <= old_texts):
                    raise ValueError(
                        f"collection cell conflict at position {position}"
                    )
            if _richness(cell) > _richness(existing):
                self._cells[position] = cell
        self._window_offset = offset

    def _longest_overlap(
        self,
        right: list[dict[str, Any]],
    ) -> tuple[int, int] | None:
        left_keys = [str(cell.get("content_key") or "") for cell in self._cells]
        right_keys = [str(cell.get("content_key") or "") for cell in right]
        best: tuple[int, int, int] | None = None
        for left_index, left_key in enumerate(left_keys):
            for right_index, right_key in enumerate(right_keys):
                if not left_key or left_key != right_key:
                    continue
                offset = left_index - right_index
                if offset < self._window_offset:
                    continue
                size = 0
                while (
                    left_index + size < len(left_keys)
                    and right_index + size < len(right_keys)
                    and left_keys[left_index + size] == right_keys[right_index + size]
                ):
                    size += 1
                candidate = (size, offset, left_index)
                if size and (best is None or candidate > best):
                    best = candidate
        return (best[1], best[2]) if best else None

    @property
    def cells(self) -> list[dict[str, Any]]:
        return list(self._cells)


def _default_projection(request: dict[str, Any], schema: type[BaseModel]) -> BaseModel:
    config = resolve_llm_config("observation")
    llm = ChatOpenAI(
        model=config.model,
        api_key=config.api_key,
        base_url=config.base_url,
        timeout=config.timeout_s,
        max_retries=config.max_retries,
        temperature=0,
        extra_body=dashscope_extra_body(config.model),
    )
    return invoke_structured(
        llm,
        [
            SystemMessage(content=_SYSTEM),
            HumanMessage(content=json.dumps(request, ensure_ascii=False)),
        ],
        schema,
        trace_label="statement.acquire.collection_projection",
    )


def _record_segments(
    cells: list[dict[str, Any]],
    fields: list[str],
    project: ProjectionCall,
    field_types: dict[str, str] | None = None,
    goal: str = "",
) -> list[list[dict[str, Any]]]:
    summary = [
        {
            "cell": f"c{index}",
            "structural_key": cell.get("structural_key"),
            "texts": cell.get("texts") or [],
            "controls": [
                control.get("label")
                for control in cell.get("controls") or []
                if control.get("label")
            ],
        }
        for index, cell in enumerate(cells)
    ]
    occurrences: dict[str, list[int]] = {}
    for index, item in enumerate(summary):
        occurrences.setdefault(str(item["structural_key"]), []).append(index)
    candidates = []
    for indexes in occurrences.values():
        if len(indexes) < 2:
            continue
        bounds = [
            (start, indexes[index + 1] if index + 1 < len(indexes) else len(summary))
            for index, start in enumerate(indexes[:2])
        ]
        try:
            for start, end in bounds:
                _source_catalog(cells[start:end], "", fields, field_types)
        except ValueError:
            continue
        else:
            candidates.append({
                "anchor_cell": f"c{indexes[0]}",
                "sample_records": [summary[start:end] for start, end in bounds],
            })
    if not candidates:
        candidates = [
            {"anchor_cell": f"c{index}", "sample_records": [summary[index:]]}
            for index in range(len(summary))
        ]
    selected = project(
        {
            "mode": "record_anchor",
            "collection_goal": goal,
            "requested_fields": fields,
            "requested_field_types": field_types or {},
            "ordered_cells": summary,
            "anchor_candidates": candidates,
        },
        _Projection,
    )
    if not isinstance(selected, _Projection):
        raise TypeError("record anchor projection returned the wrong schema")
    if not selected.anchor_cell:
        return []
    try:
        selected_index = int(selected.anchor_cell.removeprefix("c"))
        anchor_key = cells[selected_index]["structural_key"]
    except (IndexError, KeyError, ValueError):
        raise ValueError(f"invalid record anchor {selected.anchor_cell!r}") from None
    anchors = [
        index
        for index, cell in enumerate(cells)
        if cell.get("structural_key") == anchor_key
    ]
    return [
        cells[start:(anchors[index + 1] if index + 1 < len(anchors) else len(cells))]
        for index, start in enumerate(anchors)
    ]


def _text_spans(text: str, *, phrases: bool) -> dict[str, bool]:
    """Return exact token spans and whether each is a single rendered token."""
    tokens = list(re.finditer(r"\S+", text))
    values = {text: True}
    for width in range(1, (len(tokens) if phrases else 1) + 1):
        for start in range(len(tokens) - width + 1):
            span = text[tokens[start].start():tokens[start + width - 1].end()]
            for value in (span, span.strip(" ,;:")):
                if value:
                    values[value] = values.get(value, False) or width == 1
    return values


def _source_catalog(
    cells: list[dict[str, Any]],
    prefix: str,
    fields: list[str],
    field_types: dict[str, str] | None,
) -> tuple[dict[str, Any], dict[str, JsonValue]]:
    include_spans = any(
        str((field_types or {}).get(field) or "auto") not in {"auto", "text"}
        for field in fields
    )
    raw_values: dict[JsonValue, bool] = {}
    for cell in cells:
        for raw in cell.get("texts") or []:
            text = str(raw)
            for value, direct in _text_spans(text, phrases=include_spans).items():
                raw_values[value] = raw_values.get(value, False) or direct
        for control in cell.get("controls") or []:
            for key in ("label", "value", "selected"):
                value = control.get(key)
                if value is not None and not isinstance(value, (dict, list)):
                    raw_values[value] = True

    values: dict[str, JsonValue] = {}
    direct_refs: set[str] = set()
    for value, direct in raw_values.items():
        ref = f"{prefix}s{len(values)}"
        values[ref] = value
        if direct:
            direct_refs.add(ref)
    candidates = _field_candidates(values, direct_refs, fields, field_types)
    selectable = {ref for refs in candidates.values() for ref in refs}
    return {
        "sources": [
            {"source_ref": ref, "value": value}
            for ref, value in values.items() if ref in selectable
        ],
        "field_candidates": candidates,
    }, values


def _field_candidates(
    values: dict[str, JsonValue],
    direct_refs: set[str],
    fields: list[str],
    field_types: dict[str, str] | None,
) -> dict[str, list[str]]:
    from gui_agent.core.run.statements.compute_kernel import (
        ComputeKernelError,
        normalize_table_value,
    )

    candidates: dict[str, list[str]] = {}
    for field in fields:
        value_type = str((field_types or {}).get(field) or "auto")
        refs = []
        for ref, value in values.items():
            if value_type in {"auto", "text"} and ref not in direct_refs:
                continue
            try:
                normalize_table_value(field, value, value_type)  # type: ignore[arg-type]
                refs.append(ref)
            except ComputeKernelError:
                pass
        if not refs:
            raise ValueError(
                f"no source compatible with field {field!r} type {value_type!r}"
            )
        candidates[field] = refs
    return candidates


def _project_records(
    segments: list[list[dict[str, Any]]],
    fields: list[str],
    project: ProjectionCall,
    field_types: dict[str, str] | None = None,
) -> list[dict[str, JsonValue]]:
    catalogs: list[dict[str, Any]] = []
    values: dict[str, JsonValue] = {}
    for index, cells in enumerate(segments):
        record = f"r{index}"
        catalog, record_values = _source_catalog(
            cells, f"{record}.", fields, field_types,
        )
        catalogs.append({"record": record, **catalog})
        values.update(record_values)
    selected = project(
        {
            "mode": "field_sources",
            "requested_fields": fields,
            "requested_field_types": field_types or {},
            "records": catalogs,
        },
        _Projection,
    )
    if not isinstance(selected, _Projection):
        raise TypeError("field source projection returned the wrong schema")
    by_record = {item["record"]: item for item in catalogs}
    projected: dict[str, dict[str, str]] = {}
    for record in selected.records:
        catalog = by_record.get(record.record)
        if catalog is None or record.record in projected:
            raise ValueError(f"invalid projected record {record.record!r}")
        refs = {item.field: item.source_ref for item in record.fields}
        if len(refs) != len(record.fields) or set(refs) != set(fields):
            raise ValueError("projected fields do not match the requested schema")
        candidates = catalog["field_candidates"]
        if any(ref not in candidates[field] for field, ref in refs.items()):
            raise ValueError("projected field source is incompatible with its declared type")
        projected[record.record] = refs
    if set(projected) != set(by_record):
        raise ValueError("projected records do not match the input records")
    return [
        {field: values[projected[item["record"]][field]] for field in fields}
        for item in catalogs
    ]


def materialize_cell_records(
    cells: Sequence[dict[str, Any]],
    fields: Sequence[str],
    *,
    project: ProjectionCall | None = None,
    field_types: dict[str, str] | None = None,
    goal: str = "",
) -> list[dict[str, JsonValue]]:
    """Project records while copying every output value from an exact source ref."""
    requested = [str(field) for field in fields]
    if not requested or len(requested) != len(set(requested)):
        raise ValueError("cell projection requires unique requested fields")
    ordered = [dict(cell) for cell in cells]
    invoke = project or _default_projection
    if not ordered:
        return []
    segments = _record_segments(ordered, requested, invoke, field_types, goal)
    if not segments:
        return []
    rows: list[dict[str, JsonValue]] = []
    seen: set[str] = set()
    for row in _project_records(segments, requested, invoke, field_types):
        identity = json.dumps(row, ensure_ascii=False, sort_keys=True)
        if identity not in seen:
            rows.append(row)
            seen.add(identity)
    return rows


__all__ = ["CellStream", "materialize_cell_records"]
