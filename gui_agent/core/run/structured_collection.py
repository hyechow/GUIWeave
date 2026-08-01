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


class _Projection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    anchor_cell: str = ""
    fields: list[_FieldSource] = Field(default_factory=list)


ProjectionCall = Callable[[dict[str, Any], type[BaseModel]], BaseModel]


def _richness(cell: dict[str, Any]) -> tuple[int, int, int]:
    return (
        int(not cell.get("clipped_top") and not cell.get("clipped_bottom")),
        len(" ".join(str(value) for value in cell.get("texts") or [])),
        len(cell.get("controls") or []),
    )


@dataclass
class CellStream:
    """Join consecutive cell windows only when their exact overlap is unique."""

    _slots: dict[int, dict[str, Any]] = field(default_factory=dict)
    _previous: list[dict[str, Any]] = field(default_factory=list)
    _previous_offset: int = 0

    def add(self, window: Sequence[dict[str, Any]]) -> None:
        current = [dict(cell) for cell in window]
        offset = 0 if not self._previous else self._overlap_offset(current)
        for index, cell in enumerate(current):
            position = offset + index
            existing = self._slots.get(position)
            if existing is not None and existing.get("content_key") != cell.get("content_key"):
                old_texts = set(existing.get("texts") or [])
                new_texts = set(cell.get("texts") or [])
                if not (old_texts <= new_texts or new_texts <= old_texts):
                    raise ValueError(
                        f"collection cell conflict at position {position}"
                    )
            if existing is None or _richness(cell) > _richness(existing):
                self._slots[position] = cell
        self._previous = current
        self._previous_offset = offset

    def _overlap_offset(self, current: list[dict[str, Any]]) -> int:
        left = [str(cell.get("content_key") or "") for cell in self._previous]
        right = [str(cell.get("content_key") or "") for cell in current]
        best = 0
        matches: list[tuple[int, int]] = []
        for left_index in range(len(left)):
            for right_index in range(len(right)):
                size = 0
                while (
                    left_index + size < len(left)
                    and right_index + size < len(right)
                    and left[left_index + size] == right[right_index + size]
                ):
                    size += 1
                if size > best:
                    best = size
                    matches = [(left_index, right_index)]
                elif size == best and size:
                    matches.append((left_index, right_index))
        offsets = {
            self._previous_offset + left_index - right_index
            for left_index, right_index in matches
            if self._previous_offset + left_index - right_index
            >= self._previous_offset
        }
        if best == 0 or not offsets:
            raise ValueError("collection windows do not have one exact continuous overlap")
        return max(offsets)

    @property
    def cells(self) -> list[dict[str, Any]]:
        return [self._slots[position] for position in sorted(self._slots)]


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
    selected = project(
        {
            "mode": "record_anchor",
            "requested_fields": fields,
            "ordered_cells": summary,
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


def _source_catalog(
    cells: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, JsonValue]]:
    catalog: list[dict[str, Any]] = []
    values: dict[str, JsonValue] = {}
    for cell_index, cell in enumerate(cells):
        sources: list[dict[str, JsonValue]] = []
        for text_index, raw in enumerate(cell.get("texts") or []):
            value = str(raw)
            ref = f"c{cell_index}.t{text_index}"
            values[ref] = value
            sources.append({"source_ref": ref, "value": value})
            for token_index, token in enumerate(re.findall(r"\S+", value)):
                if token == value:
                    continue
                token_ref = f"{ref}.p{token_index}"
                values[token_ref] = token
                sources.append({"source_ref": token_ref, "value": token})
        for control_index, control in enumerate(cell.get("controls") or []):
            for key in ("label", "value", "selected"):
                value = control.get(key)
                if value is None or isinstance(value, (dict, list)):
                    continue
                ref = f"c{cell_index}.k{control_index}.{key}"
                values[ref] = value
                sources.append({"source_ref": ref, "value": value})
        catalog.append({"cell": f"c{cell_index}", "sources": sources})
    return catalog, values


def _project_record(
    cells: list[dict[str, Any]],
    fields: list[str],
    project: ProjectionCall,
) -> dict[str, JsonValue]:
    catalog, values = _source_catalog(cells)
    selected = project(
        {
            "mode": "field_sources",
            "requested_fields": fields,
            "record_cells": catalog,
        },
        _Projection,
    )
    if not isinstance(selected, _Projection):
        raise TypeError("field source projection returned the wrong schema")
    refs: dict[str, str] = {}
    for item in selected.fields:
        if item.field in refs:
            raise ValueError(f"duplicate projected field {item.field!r}")
        refs[item.field] = item.source_ref
    if set(refs) != set(fields):
        raise ValueError("projected fields do not match the requested schema")
    unknown = [ref for ref in refs.values() if ref not in values]
    if unknown:
        raise ValueError(f"projection returned unknown source refs: {unknown}")
    return {field: values[refs[field]] for field in fields}


def materialize_cell_records(
    cells: Sequence[dict[str, Any]],
    fields: Sequence[str],
    *,
    project: ProjectionCall | None = None,
) -> list[dict[str, JsonValue]]:
    """Project records while copying every output value from an exact source ref."""
    requested = [str(field) for field in fields]
    if not requested or len(requested) != len(set(requested)):
        raise ValueError("cell projection requires unique requested fields")
    ordered = [dict(cell) for cell in cells]
    invoke = project or _default_projection
    rows: list[dict[str, JsonValue]] = []
    seen: set[str] = set()
    for segment in _record_segments(ordered, requested, invoke):
        row = _project_record(segment, requested, invoke)
        identity = json.dumps(row, ensure_ascii=False, sort_keys=True)
        if identity not in seen:
            rows.append(row)
            seen.add(identity)
    return rows


__all__ = ["CellStream", "materialize_cell_records"]
