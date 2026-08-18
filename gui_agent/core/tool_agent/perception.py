"""Automatic perception materialization for vision-only and enhanced modes."""

from __future__ import annotations

import hashlib
import html
import json
import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Literal

from jsonschema import ValidationError, validate
from langchain_core.messages import HumanMessage, SystemMessage

from gui_agent.core.config import resolve_llm_config
from gui_agent.core.runtime.clock import PlatformTimeSnapshot, host_time_fallback
from gui_agent.core.tool_agent.filter_state import (
    FilterPredicate,
    canonical_filter_field,
    canonical_filter_value,
    compile_filter_predicates,
    diff_filter_sets,
    display_filter_predicates,
    match_filter_state,
)
from gui_agent.core.tool_agent.data_normalization import (
    ValueNormalizationError,
    json_value,
    normalize_table_value,
)
from gui_agent.prompts import load_prompt_text
from gui_agent.core.tool_agent.contracts import (
    DataRequirement,
    MaterializedFrame,
)
from gui_agent.core.tool_agent.data_store import RuntimeDataStore
from gui_agent.core.tool_agent.protocol import (
    diagnostic_prompt_reports,
    image_message,
    parse_json_object,
    response_usage,
)
from gui_agent.core.vision.frame_analysis import is_blank_screen
from llm.provider_config import build_chat_model, chat_request_kwargs

PerceptionMode = Literal["vision-only", "enhanced"]

_SCREEN_READER_SYSTEM = load_prompt_text("task.tool_agent.screen_reader")


class DataNormalizationError(ValueError):
    """A declared runtime field type could not be normalized losslessly."""


@dataclass
class _DetailCollectionState:
    """Private partial rows assembled across one list/detail traversal."""

    rows: list[dict[str, Any]]
    detail_fields: set[str]
    surface: str
    location: str
    pending_index: int | None = None
    scope_key: str = ""


def _normalize_runtime_value(
    field_name: str,
    value: Any,
    value_type: str,
) -> Any:
    """Normalize an observed value into the JSON form stored by Tool Agent."""

    return json_value(normalize_table_value(field_name, value, value_type))


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.casefold())


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.casefold()))


def _table_fields(table: dict[str, Any]) -> dict[str, str]:
    labels = list(table.get("headers") or [])
    labels.extend(
        key for row in table.get("rows") or [] if isinstance(row, dict) for key in row
    )
    return {
        normalized: str(label) for label in labels
        if (normalized := _normalize(str(label)))
    }


def _source_keys(
    requirement: DataRequirement,
    table: dict[str, Any],
) -> dict[str, str]:
    """Resolve exact fields and unambiguous caption-qualified aliases once."""

    available = _table_fields(table)
    caption_words = _words(str(table.get("caption") or ""))
    caption_words |= {word[:-1] for word in caption_words if word.endswith("s")}
    resolved: dict[str, str] = {}
    for field in requirement.row_schema.get("properties") or {}:
        source = requirement.field_sources.get(field, field)
        exact = _normalize(source)
        if exact in available:
            resolved[field] = exact
            continue
        source_words = _words(source)
        matches = [
            key for key, label in available.items()
            if (words := _words(label))
            and words < source_words
            and (source_words - words).issubset(caption_words)
        ]
        if len(matches) == 1:
            resolved[field] = matches[0]
    return resolved


def _structured_surface_descriptor(table: dict[str, Any]) -> dict[str, Any]:
    """Project page-wide table presence without exposing its row values.

    Operator Workers need to know when an apply/query action materialized a result
    below the screenshot fold even when they have no data requirement. Rows remain
    private to RuntimeDataStore; this descriptor carries only structure and coverage.
    """

    rows = list(table.get("rows") or [])
    traversal = table.get("traversal")
    descriptor: dict[str, Any] = {
        "kind": "rendered_data_surface",
        "rendered": True,
        "caption": str(table.get("caption") or "").strip(),
        "fields": [str(field) for field in list(table.get("headers") or [])],
        "row_count": len(rows),
        "partial": bool(table.get("partial")),
    }
    if table.get("viewport_pos") not in (None, ""):
        descriptor["viewport_position"] = table["viewport_pos"]
    if table.get("total_records") not in (None, ""):
        descriptor["total_records"] = table["total_records"]
    if isinstance(traversal, dict):
        descriptor["traversal"] = dict(traversal)
    return descriptor


def _visible_collection_regions(regions: Any) -> list[dict[str, Any]]:
    """Project exact current-frame cell text without inferring record boundaries."""
    result: list[dict[str, Any]] = []
    for region in regions or ():
        cells = [
            {
                "ref": cell.ref,
                "bounds": cell.bounds,
                "texts": cell.texts,
                "clipped_top": cell.clipped_top,
                "clipped_bottom": cell.clipped_bottom,
            }
            for cell in region.cells
            if cell.texts
        ]
        if not cells:
            continue
        result.append({
            "caption": str(region.caption or ""),
            "bounds": region.bounds,
            "traversal": dict(region.traversal),
            "viewport_tail_clipped": bool(cells[-1]["clipped_bottom"]),
            "cells": cells,
        })
    return result


def _table_supports_requirement(
    requirement: DataRequirement,
    table: dict[str, Any],
) -> bool:
    properties = requirement.row_schema.get("properties") or {}
    return bool(properties) and len(_source_keys(requirement, table)) == len(properties)


def _authoritative_empty_table(table: dict[str, Any]) -> bool:
    """Whether a structured surface proves that its current query returned zero rows.

    An empty surface can satisfy any row schema vacuously, even when the surface
    does not expose fields that would only exist on linked detail pages.  It is
    authoritative only when the adapter reports a complete, non-partial end state.
    """

    if list(table.get("rows") or []) or table.get("total_records") != 0:
        return False
    if bool(table.get("partial")):
        return False
    traversal = table.get("traversal") if isinstance(table.get("traversal"), dict) else {}
    return bool(
        traversal.get("type") == "static"
        or traversal.get("has_next_page") is False
        or traversal.get("page_index") not in (None, "")
        and traversal.get("page_count") not in (None, "")
        and traversal.get("page_index") == traversal.get("page_count")
    )


def _match_table(requirement: DataRequirement, tables: list[dict[str, Any]]) -> dict[str, Any] | None:
    target = _normalize(requirement.target_label)
    wanted_words = _words(requirement.target_label or requirement.description)
    field_count = len(requirement.row_schema.get("properties") or {})
    ranked: list[tuple[tuple[bool, bool, int, int], dict[str, Any]]] = []
    for table in tables:
        caption = _normalize(str(table.get("caption") or ""))
        caption_words = _words(str(table.get("caption") or ""))
        field_matches = len(_source_keys(requirement, table))
        rank = (
            bool(target and (target == caption or target in caption or caption in target)),
            bool(field_count) and field_matches == field_count,
            field_matches,
            len(wanted_words.intersection(caption_words)),
        )
        ranked.append((rank, table))
    if not ranked:
        return None
    rank, table = max(ranked, key=lambda item: item[0])
    return table if any(rank) else None


def _structured_rows(
    requirement: DataRequirement,
    table: dict[str, Any],
) -> list[dict[str, Any]]:
    source_map = requirement.field_sources
    properties = requirement.row_schema.get("properties") or {}
    source_keys = _source_keys(requirement, table)
    rows: list[dict[str, Any]] = []
    for row_index, source_row in enumerate(list(table.get("rows") or []), start=1):
        if not isinstance(source_row, dict):
            continue
        normalized_sources = {_normalize(str(key)): value for key, value in source_row.items()}
        output: dict[str, Any] = {}
        for field, field_schema in properties.items():
            source = source_map.get(field, field)
            value = normalized_sources.get(source_keys.get(field, ""))
            declared_type = requirement.field_types.get(field)
            if value is not None and declared_type is not None:
                try:
                    value = _normalize_runtime_value(source, value, declared_type)
                except ValueNormalizationError as exc:
                    raise DataNormalizationError(
                        f"row {row_index} field {field!r} cannot normalize as "
                        f"{declared_type}"
                    ) from exc
            elif value is not None:
                json_type = field_schema.get("type") if isinstance(field_schema, dict) else None
                if json_type in {"integer", "number"}:
                    try:
                        value = (
                            int(str(value).replace(",", ""))
                            if json_type == "integer"
                            else float(str(value).replace(",", ""))
                        )
                    except ValueError:
                        pass
            output[field] = value
        try:
            validate(instance=output, schema=requirement.row_schema)
        except ValidationError:
            # A list surface may expose only candidate fields while required values
            # live on a linked detail surface. Incomplete rows are not data yet;
            # leave the requirement open so the GUI Worker can navigate for them.
            continue
        rows.append(output)
    return rows


def _partial_structured_rows(
    requirement: DataRequirement,
    table: dict[str, Any],
) -> tuple[list[dict[str, Any]], set[str]]:
    """Read candidate fields without pretending detail-only fields were observed."""

    properties = requirement.row_schema.get("properties") or {}
    list_fields = set(_source_keys(requirement, table))
    if not list_fields:
        return [], set()
    if len(list_fields) == 1:
        only_schema = properties[next(iter(list_fields))]
        if not isinstance(only_schema, dict) or only_schema.get("type") != "string":
            return [], set()
    required_fields = set(requirement.row_schema.get("required") or [])
    # Every declared projection field absent from the list belongs to linked
    # detail acquisition. ``required`` controls final JSON validation; it must
    # not silently erase a field the Master explicitly asked to collect.
    detail_fields = set(properties).difference(list_fields)
    candidate_requirement = requirement.model_copy(update={"row_schema": {
        "type": "object",
        "properties": {field: properties[field] for field in list_fields},
        "required": sorted(required_fields.intersection(list_fields)),
        "additionalProperties": False,
    }})
    return _structured_rows(candidate_requirement, table), detail_fields


def _control_row(
    requirement: DataRequirement,
    controls: list[dict[str, Any]],
) -> tuple[dict[str, Any], set[str]]:
    """Project current form values by declared visible labels, preserving emptiness."""

    by_label = {
        _normalize(str(control.get("label") or "")): control
        for control in reversed(controls)
        if control.get("label")
    }
    row: dict[str, Any] = {}
    observed: set[str] = set()
    for field in (requirement.row_schema.get("properties") or {}):
        source = requirement.field_sources.get(field, field)
        control = by_label.get(_normalize(source))
        if control is None:
            continue
        missing = object()
        value = next(
            (control[key] for key in ("selected_text_primary", "selected_text", "value") if key in control),
            missing,
        )
        if value is missing:
            continue
        observed.add(field)
        if isinstance(value, str):
            value = html.unescape(value).strip()
        declared_type = requirement.field_types.get(field)
        if value not in (None, "") and declared_type is not None:
            value = _normalize_runtime_value(source, value, declared_type)
        row[field] = "" if value is None else value
    return row, observed


def _nonempty(value: Any) -> bool:
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


def _normalize_visual_rows(
    requirement: DataRequirement,
    rows: list[Any],
) -> list[dict[str, Any]]:
    normalized_rows: list[dict[str, Any]] = []
    for row_index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            if requirement.field_types:
                raise DataNormalizationError(
                    f"visual row {row_index} is not an object"
                )
            continue
        normalized = dict(row)
        required_fields = set(requirement.row_schema.get("required") or [])
        properties = requirement.row_schema.get("properties") or {}
        for field, value in list(normalized.items()):
            field_schema = properties.get(field)
            allowed_type = (
                field_schema.get("type") if isinstance(field_schema, dict) else None
            )
            allows_null = allowed_type == "null" or (
                isinstance(allowed_type, list) and "null" in allowed_type
            )
            if value is None and field not in required_fields and not allows_null:
                # Vision providers commonly return explicit null for an optional
                # field that is absent on the surface. In JSON Schema that means
                # "not observed", so omit it instead of rejecting the whole row.
                normalized.pop(field)
        for field, declared_type in requirement.field_types.items():
            if field not in normalized or normalized[field] is None:
                continue
            source = requirement.field_sources.get(field, field)
            try:
                normalized[field] = _normalize_runtime_value(
                    source,
                    normalized[field],
                    declared_type,
                )
            except ValueNormalizationError as exc:
                raise DataNormalizationError(
                    f"visual row {row_index} field {field!r} cannot normalize as "
                    f"{declared_type}"
                ) from exc
        try:
            validate(instance=normalized, schema=requirement.row_schema)
        except ValidationError:
            # Missing/detail-only fields mean collection is incomplete, not that a
            # declared source value failed normalization.
            continue
        normalized_rows.append(normalized)
    return normalized_rows


def _exact_filter_row_values(
    requirement: DataRequirement,
    filters: dict[str, Any],
) -> dict[str, Any]:
    """Return equality filter values that Runtime may safely attach to each row."""
    properties = requirement.row_schema.get("properties") or {}
    return {
        field: value for field, value in filters.items()
        if field in properties
        and requirement.filters.get(field) == value
        and isinstance(value, (str, int, float, bool))
    }


def _visible_row_schema(
    requirement: DataRequirement,
    filters: dict[str, Any],
) -> dict[str, Any]:
    """Omit only row fields proven by the logical filter and confirmed UI scope."""
    supplied = set(_exact_filter_row_values(requirement, filters))
    return {
        **requirement.row_schema,
        "properties": {field: schema for field, schema in (
            requirement.row_schema.get("properties") or {}
        ).items() if field not in supplied},
        "required": [],
    }


def _visual_scope_state(
    states: dict[tuple[str, str, str], str],
    key: tuple[str, str, str],
    extracted: dict[str, Any],
    *,
    has_result: bool,
) -> bool | None:
    """Advance one filter scope from visible selection through committed results."""

    if extracted.get("scope_satisfied") is False:
        states.pop(key, None)
        return False
    visible = extracted.get("filter_state_visible") is True
    if visible:
        states[key] = (
            "pending" if extracted.get("filter_commit_pending") is True else "confirmed"
        )
    elif states.get(key) == "pending" and has_result:
        states[key] = "confirmed"
    elif states.get(key) == "confirmed" and not has_result:
        states.pop(key)
    return True if states.get(key) == "confirmed" else None


def _compare_values(
    left: Any,
    right: Any,
    *,
    field_name: str = "",
    field_type: str | None = None,
) -> int:
    if field_type is not None:
        try:
            left = _normalize_runtime_value(field_name, left, field_type)
            right = _normalize_runtime_value(field_name, right, field_type)
        except ValueNormalizationError as exc:
            raise DataNormalizationError(
                f"filter field {field_name!r} cannot normalize as {field_type}"
            ) from exc
    left_value = canonical_filter_value(left)
    right_value = canonical_filter_value(right)
    try:
        left_decimal = Decimal(str(left_value))
        right_decimal = Decimal(str(right_value))
    except (InvalidOperation, ValueError):
        left_text, right_text = str(left_value), str(right_value)
        return (left_text > right_text) - (left_text < right_text)
    return (left_decimal > right_decimal) - (left_decimal < right_decimal)


def _is_semantic_predicate(
    predicate: FilterPredicate,
    *,
    field_type: str | None,
) -> bool:
    """True when the predicate is semantic free-text only perception can judge.

    A `contains` operator (from a ``<field>_contains`` filter key) is always
    semantic. An exact predicate on a ``text`` field whose value is a multi-token
    phrase is likewise prose ("records that mention X"), not an identity label:
    deterministic string equality cannot prove a paraphrase miss, so it defers to
    perception rather than hard-rejecting the row.
    """
    if predicate.operator == "contains":
        return True
    if predicate.operator != "eq" or field_type != "text":
        return False
    value = predicate.values[0]
    if not isinstance(value, str):
        return False
    return " " in value


def _text_contains(value: Any, phrase: Any) -> bool:
    """Casefolded substring match used by semantic predicates."""
    text = canonical_filter_value(value)
    needle = canonical_filter_value(phrase)
    return isinstance(text, str) and isinstance(needle, str) and needle in text


def _requirement_has_semantic_predicate(requirement: DataRequirement) -> bool:
    """True when any filter predicate defers to perception (see ``_is_semantic_predicate``)."""
    predicates = compile_filter_predicates(requirement.filters)
    raw_by_canonical = {
        canonical_filter_field(field): field for field in (requirement.filters or {})
    }
    for canonical_field, predicate in predicates.items():
        raw_field = raw_by_canonical.get(canonical_field, "")
        field_type = requirement.field_types.get(raw_field)
        if _is_semantic_predicate(predicate, field_type=field_type):
            return True
    return False


def _rows_satisfy_filters(
    requirement: DataRequirement,
    rows: list[dict[str, Any]],
    *,
    filters: dict[str, Any] | None = None,
) -> bool | None:
    active_filters = requirement.filters if filters is None else filters
    if not active_filters:
        return True
    if not rows:
        return None
    predicates = compile_filter_predicates(active_filters)
    row_fields = {
        canonical_filter_field(field): field for field in active_filters
    }
    unknown = False
    for row in rows:
        for field, predicate in predicates.items():
            row_field = row_fields.get(field, field.replace(" ", "_"))
            if row_field not in row:
                unknown = True
                continue
            value = row[row_field]
            field_type = requirement.field_types.get(row_field)
            field_name = requirement.field_sources.get(row_field, row_field)
            if _is_semantic_predicate(predicate, field_type=field_type):
                # A literal substring match is decisive; absence is NOT proof of a
                # miss (a paraphrase is a valid match). Defer to perception.
                if _text_contains(value, predicate.values[0]):
                    continue
                unknown = True
                continue
            if predicate.operator == "eq":
                matches = _compare_values(
                    value,
                    predicate.values[0],
                    field_name=field_name,
                    field_type=field_type,
                ) == 0
            elif predicate.operator == "gte":
                matches = _compare_values(
                    value,
                    predicate.values[0],
                    field_name=field_name,
                    field_type=field_type,
                ) >= 0
            elif predicate.operator == "lte":
                matches = _compare_values(
                    value,
                    predicate.values[0],
                    field_name=field_name,
                    field_type=field_type,
                ) <= 0
            else:
                matches = (
                    _compare_values(
                        value,
                        predicate.values[0],
                        field_name=field_name,
                        field_type=field_type,
                    ) >= 0
                    and _compare_values(
                        value,
                        predicate.values[1],
                        field_name=field_name,
                        field_type=field_type,
                    ) <= 0
                )
            if not matches:
                return False
    return None if unknown else True


def _logical_rows(
    requirement: DataRequirement,
    rows: list[dict[str, Any]],
    *,
    keep_unknown: bool = False,
) -> list[dict[str, Any]]:
    return [row for row in rows
            if (match := _rows_satisfy_filters(requirement, [row])) is True
            or keep_unknown and match is None]


_TEXT_QUERY_CONTROL_KINDS = {
    "text_input", "textbox", "searchbox", "combobox", "editable_text",
}


def _text_query_fields(controls: list[dict[str, Any]]) -> set[str]:
    """Return UI fields whose values are recall queries, not exact predicates."""

    fields: set[str] = set()
    for control in controls:
        if (
            control.get("is_filter") is not True
            or control.get("is_datepicker") is True
            or str(control.get("kind") or "").casefold()
            not in _TEXT_QUERY_CONTROL_KINDS
        ):
            continue
        for key in ("label", "name", "placeholder"):
            field = canonical_filter_field(control.get(key))
            if field:
                fields.add(field)
    return fields


def _is_text_query_field(field: str, query_fields: set[str]) -> bool:
    return any(
        field == candidate or field in candidate or candidate in field
        for candidate in query_fields
    )


def _scope_descriptor(
    requirement: DataRequirement,
    *,
    required_predicates: dict[str, Any],
    applied_filter_state: Any,
    applied_filters: dict[str, Any],
    text_query_fields: set[str],
    rows: list[dict[str, Any]],
    visual_scope_satisfied: bool | None = None,
) -> dict[str, Any]:
    required_ui_predicates = {
        requirement.field_sources.get(field, field): value
        for field, value in required_predicates.items()
    }
    required = compile_filter_predicates(required_ui_predicates)
    applied = (
        dict(applied_filter_state.predicates)
        if applied_filter_state is not None
        and getattr(applied_filter_state, "coverage", "") == "complete"
        else compile_filter_predicates(applied_filters)
    )
    query_fields = {
        canonical_filter_field(field) for field in text_query_fields if field
    }
    query_state = {
        "filters": dict(applied_filters),
        "text_fields": sorted(query_fields),
    }
    if not required:
        has_applied_filters = bool(applied)
        return {
            "status": "unmet" if has_applied_filters else "met",
            "requested_filters": {},
            "required_predicates": {},
            "applied_filters": dict(applied_filters),
            "query_state": query_state,
            "query_outcome": "unobserved",
            "empty_authoritative": not has_applied_filters,
            "evidence": (
                "unexpected_applied_filters"
                if has_applied_filters
                else "no_filter_required"
            ),
        }
    exact_applied_scope = match_filter_state(required, applied_filter_state) == "met"
    required_fields = set(required)
    filter_diff = diff_filter_sets(required, applied)
    hard_conflicts = {
        field for field in filter_diff["conflicting"]
        if not _is_text_query_field(field, query_fields)
    }
    hard_extras = {
        field for field in filter_diff["extra"]
        if not _is_text_query_field(field, query_fields)
    }
    hard_blocked = bool(hard_conflicts or hard_extras)
    semantic_scope = _requirement_has_semantic_predicate(requirement)
    has_valid_candidate = any(
        _rows_satisfy_filters(requirement, [row]) is True for row in rows
    )
    exact_scope_is_query = any(
        _is_text_query_field(field, query_fields) for field in required_fields
    )
    empty_authoritative = bool(
        exact_applied_scope and not exact_scope_is_query and not hard_blocked
    )
    if hard_blocked:
        status, evidence = "unmet", "conflicting_non_query_filters"
    elif visual_scope_satisfied is False and not semantic_scope:
        # For a semantic free-text predicate there is no UI filter to apply;
        # perception's scope_satisfied=False is informational, not a hard block
        # (it would otherwise drive a worker to seek a nonexistent filter and
        # report blocked, triggering a strategy replacement churn).
        status, evidence = "unmet", "visual_filter_state"
    elif has_valid_candidate:
        status, evidence = "met", "validated_candidates"
    elif exact_applied_scope:
        status, evidence = "met", (
            "text_query_state" if exact_scope_is_query else "applied_filter_state"
        )
    else:
        status, evidence = "unknown", (
            "query_recall_state" if applied or query_fields else "unavailable"
        )
    return {
        "status": status,
        "requested_filters": display_filter_predicates(required_ui_predicates),
        "required_predicates": display_filter_predicates(required_ui_predicates),
        "applied_filters": dict(applied_filters),
        "query_state": query_state,
        "query_outcome": "unobserved",
        "empty_authoritative": empty_authoritative,
        "evidence": evidence,
    }


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()[:16]


def _surface_marker(table: dict[str, Any] | None) -> Any:
    if table is None:
        return []
    path = table.get("path")
    location = table.get("location")
    return [path, location] if path and location else (
        path or table.get("caption") or table.get("headers") or []
    )


def _collection_key(
    table: dict[str, Any],
    requirement: DataRequirement,
    scope: dict[str, Any],
    url: str,
) -> str:
    return "surface:" + _fingerprint({
        "surface": _surface_marker(table),
        "route": url,
        "schema": requirement.row_schema,
        "filters": scope.get("applied_filters") or scope.get("requested_filters") or {},
    })


def _visual_collection_key(requirement: DataRequirement, scope: dict[str, Any]) -> str:
    return "visual:" + _fingerprint({
        "requirement": requirement.id,
        "filters": scope["requested_filters"],
    })


def _structured_coverage(
    table: dict[str, Any],
    requirement: DataRequirement,
    *,
    scope: dict[str, Any],
    url: str,
) -> dict[str, Any]:
    traversal = table.get("traversal") if isinstance(table.get("traversal"), dict) else {}
    page_index = traversal.get("page_index")
    page_count = traversal.get("page_count")
    has_next = traversal.get("has_next_page")
    traversal_type = str(traversal.get("type") or "")
    at_end = bool(
        traversal_type == "static"
        or has_next is False
        or page_index not in (None, "")
        and page_count not in (None, "")
        and page_index == page_count
        or traversal_type == "scroll"
        and traversal.get("at_scroll_end") is True
    )
    movement_keys = {
        "type",
        "page_index",
        "page_count",
        "has_next_page",
        "has_prev_page",
        "page_size",
        "page_size_options",
        "can_scroll_more",
        "can_scroll_back",
        "at_scroll_end",
    }
    movement = {
        key: value
        for key, value in traversal.items()
        if key in movement_keys and value is not None
    }
    return {
        "scope": requirement.scope,
        "cardinality": requirement.cardinality,
        "scope_status": scope["status"],
        "requested_filters": scope["requested_filters"],
        "applied_filters": scope["applied_filters"],
        "collection_key": _collection_key(table, requirement, scope, url),
        "source_scope": "structured_surface",
        "end_visible": at_end,
        "at_end": at_end,
        "partial": bool(table.get("partial")),
        "total_records": table.get("total_records"),
        "page_index": page_index,
        "page_count": page_count,
        "has_next_page": has_next,
        "traversal_type": traversal_type,
        "movement": movement,
        "window_key": f"page:{page_index}" if page_index not in (None, "") else "",
        "window_context": _fingerprint({
            "route": url,
            "surface": _surface_marker(table),
        }),
        "start_visible": table.get("start_visible"),
    }


def _page_has_more(page_index: Any, page_count: Any) -> bool:
    try:
        return int(page_index) < int(page_count)
    except (TypeError, ValueError):
        return False


class PerceptionMaterializer:
    def __init__(
        self,
        *,
        mode: PerceptionMode,
        data_store: RuntimeDataStore,
        log_dir: Path,
        platform_time: PlatformTimeSnapshot,
        on_event: Callable[..., None] | None = None,
    ) -> None:
        self.mode = mode
        self.data_store = data_store
        self.log_dir = log_dir
        self.platform_time = platform_time
        self.task_goal = ""
        self._on_event = on_event
        self._expected_totals: dict[tuple[str, str, str], int] = {}
        self._detail_collections: dict[tuple[str, str], _DetailCollectionState] = {}
        self._visual_filter_states: dict[tuple[str, str, str], str] = {}
        cfg = resolve_llm_config("tool_agent.perception")
        self._vision_cfg = cfg
        self.model = cfg.model
        self._vision = build_chat_model(cfg)
        try:
            reader_cfg = resolve_llm_config("tool_agent.screen_reader")
        except Exception:  # noqa: BLE001 - legacy configs without the slot fall back
            reader_cfg = cfg
        self._screen_reader_cfg = reader_cfg
        self._screen_reader = build_chat_model(reader_cfg)

    def _assemble_detail_collection(
        self,
        *,
        state_key: tuple[str, str],
        requirement: DataRequirement,
        candidate_rows: list[dict[str, Any]],
        detail_fields: set[str],
        controls: list[dict[str, Any]],
        structured_rows: list[dict[str, Any]],
        scope_status: str,
        surface: str,
        location: str,
        observed_rows: list[dict[str, Any]] | None = None,
        scope_key: str = "",
    ) -> tuple[_DetailCollectionState, list[dict[str, Any]], dict[str, Any]] | None:
        """Accumulate list candidates and form details without exposing row values."""

        state = self._detail_collections.get(state_key)
        if (
            state is not None
            and scope_key
            and state.scope_key
            and state.scope_key != scope_key
        ):
            state = None
            self._detail_collections.pop(state_key, None)
        if candidate_rows and detail_fields:  # ReAct: assemble candidates without requiring scope "met"
            identity_fields = detail_fields | (state.detail_fields if state else set())

            def identities(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
                return [
                    {key: value for key, value in row.items() if key not in identity_fields}
                    for row in items
                ]

            if state is None:
                state = _DetailCollectionState(
                    rows=[dict(row) for row in candidate_rows],
                    detail_fields=set(detail_fields),
                    surface=surface,
                    location=location,
                    scope_key=scope_key,
                )
                self._detail_collections[state_key] = state
            else:
                known = identities(state.rows)
                incoming = identities(candidate_rows)
                parent = tuple(
                    part.strip().casefold()
                    for part in state.location.split(" > ") if part.strip()
                )
                child = tuple(
                    part.strip().casefold()
                    for part in location.split(" > ") if part.strip()
                )
                resolution_started = state.pending_index is not None or any(
                    all(_nonempty(row.get(field)) for field in state.detail_fields)
                    for row in state.rows
                )
                if (
                    not resolution_started
                    and parent
                    and len(child) > len(parent)
                    and child[:len(parent)] == parent
                ):
                    state.rows = [dict(row) for row in candidate_rows]
                    state.detail_fields = set(detail_fields)
                    state.surface = surface
                    state.location = location
                    state.pending_index = None
                    known = identities(state.rows)
                    incoming = known
                state.detail_fields.update(detail_fields)
                allow_new = surface == state.surface
                for candidate, identity in zip(candidate_rows, incoming, strict=True):
                    if identity in known:
                        state.rows[known.index(identity)].update(candidate)
                    elif allow_new:
                        known.append(identity)
                        state.rows.append(dict(candidate))
        if state is None or not state.rows or not state.detail_fields:
            return None

        def matching_row(detail: dict[str, Any]) -> int | None:
            scores = []
            for row in state.rows:
                score = 0
                for key, value in detail.items():
                    if key in state.detail_fields or not _nonempty(value):
                        continue
                    if row.get(key) == value:
                        score += 1
                    elif _nonempty(row.get(key)):
                        # An explicit identity contradiction vetoes the row; a
                        # single coincidental field match must not credit a
                        # stranger's details to a candidate.
                        score -= 1
                scores.append(score)
            best = max(scores, default=0)
            return scores.index(best) if best > 0 and scores.count(best) == 1 else None

        detail, observed = _control_row(requirement, controls)
        for extra in (*structured_rows, *(observed_rows or [])):
            if (target := matching_row(extra)) is not None:
                for key in state.detail_fields:
                    if _nonempty(extra.get(key)):
                        state.rows[target][key] = extra[key]
                        observed.add(key)
        current_editor = None
        if observed.intersection(state.detail_fields):

            def hard_contradiction(row: dict[str, Any]) -> bool:
                for key, value in detail.items():
                    if key in state.detail_fields or not _nonempty(value):
                        continue
                    existing = row.get(key)
                    if not _nonempty(existing) or str(existing) == str(value):
                        continue
                    a = str(existing).strip().casefold()
                    b = str(value).strip().casefold()
                    if a in b or b in a:
                        # Related identity (parent/child record such as a base
                        # SKU and its variant) is not a contradiction.
                        continue
                    return True
                return False

            matching = matching_row(detail)
            carries_identity = any(
                key not in state.detail_fields and _nonempty(value)
                for key, value in detail.items()
            )
            if matching is not None:
                target = matching
            else:
                target = state.pending_index
                if (
                    target is not None
                    and carries_identity
                    and hard_contradiction(state.rows[target])
                ):
                    # An editor naming an unrelated identity is a stranger:
                    # credit nothing, keep the pending walk untouched.
                    target = None
                if target is None and not carries_identity:
                    # An identity-free editor (only detail fields visible)
                    # continues the walk: credit the first candidate still
                    # missing details.
                    target = next(
                        (
                            index
                            for index, row in enumerate(state.rows)
                            if any(
                                not _nonempty(row.get(field))
                                for field in state.detail_fields
                            )
                        ),
                        None,
                    )
            if target is not None:
                pre_resolved = all(
                    _nonempty(state.rows[target].get(field))
                    for field in state.detail_fields
                )
                for key in state.detail_fields.intersection(observed):
                    if key not in detail:
                        # Credited by a structured/observed row this frame; the
                        # control detail has no value to add for it.
                        continue
                    value = detail.get(key)
                    if matching is not None or (
                        _nonempty(value) and not _nonempty(state.rows[target].get(key))
                    ):
                        state.rows[target][key] = value
                state.pending_index = target if any(
                    not _nonempty(state.rows[target].get(field))
                    for field in state.detail_fields
                ) else None
                # record_walk's freshness gate advances only on an editor this
                # frame just completed; pre-resolved or gapped editors yield.
                current_editor = {
                    "ordinal": target + 1,
                    "pre_resolved": pre_resolved,
                    "resolved": state.pending_index is None,
                }

        unresolved_indexes = [
            index for index, row in enumerate(state.rows)
            if any(not _nonempty(row.get(field)) for field in state.detail_fields)
        ]
        progress = {
            "candidate_records": len(state.rows),
            "current_observed_detail_fields": sorted(
                state.detail_fields.intersection(observed)
            ),
            "current_editor": current_editor,
            "resolved_candidate_ordinals": [
                index + 1
                for index in range(len(state.rows))
                if index not in unresolved_indexes
            ],
            "next_unresolved_candidate": (
                {
                    "ordinal": unresolved_indexes[0] + 1,
                    "fields": {
                        key: value
                        for key, value in state.rows[unresolved_indexes[0]].items()
                        if key not in state.detail_fields and _nonempty(value)
                    },
                }
                if unresolved_indexes
                else None
            ),
        }
        rows = [dict(row) for row in state.rows] if not unresolved_indexes else []
        return state, rows, progress

    def observe(
        self,
        *,
        bundle: Any,
        platform: Any,
        requirements: list[DataRequirement],
        allow_linked_details: bool = True,
        state_scope: str = "",
        frame_no: int,
    ) -> tuple[MaterializedFrame, bytes]:
        frame_id = f"frame:{frame_no}"
        screenshot_path = self.log_dir / f"screenshot_tool_agent_{frame_no}.png"
        tables: list[dict[str, Any]] = []
        controls: list[dict[str, Any]] = []
        applied_filters: dict[str, Any] = {}
        applied_filter_state = None
        visible_collection_regions: list[dict[str, Any]] = []
        readiness: Literal["ready", "loading", "blank"] = "ready"
        readiness_reason = ""
        if self.mode == "enhanced":
            observation = bundle.make_perception(platform, screenshot_path).observe()
            png = observation.png_bytes
            tables = list(observation.tables or [])
            # Geometry is optional platform-enhanced evidence. Keeping normalized
            # control centers lets the adapter ground a visual action by exact ref;
            # vision-only mode remains independent because it supplies no controls.
            control_inventory = (
                getattr(observation, "form_control_state", None)
                or getattr(observation, "form_controls", None)
                or []
            )
            controls = [
                dict(item)
                for item in list(control_inventory)
                if isinstance(item, dict)
            ]
            applied_filters = dict(getattr(observation, "applied_filters", None) or {})
            applied_filter_state = getattr(observation, "applied_filter_state", None)
            if not requirements:
                visible_collection_regions = _visible_collection_regions(
                    getattr(observation, "collection_regions", None)
                )
            url, title = observation.url or "", observation.title or ""
            platform_loading = getattr(observation, "loading", None)
            visible_controls = any(
                item.get("in_viewport") is not False
                for item in controls
            )
            if platform_loading is True:
                readiness = "loading"
                readiness_reason = "platform reports an in-progress document or request"
            elif platform_loading is False and not tables and not visible_controls:
                try:
                    visually_blank = is_blank_screen(png)
                except Exception:
                    visually_blank = False
                if visually_blank:
                    readiness = "blank"
                    readiness_reason = (
                        "rendered browser frame is visually blank and exposes no visible structure"
                    )
        else:
            png = platform.screenshot()
            screenshot_path.parent.mkdir(parents=True, exist_ok=True)
            screenshot_path.write_bytes(png)
            client = getattr(platform, "client", None)
            url = title = ""
            if client is not None and hasattr(client, "page_info"):
                url, title = client.page_info()

        chunks = []
        collections = []
        missing = []
        expected_totals = self._expected_totals
        requirement_scopes: dict[str, dict[str, Any]] = {}
        text_query_fields = _text_query_fields(controls)
        for requirement in requirements:
            detail_key = (state_scope, requirement.id)
            required_predicates = dict(requirement.filters)
            rows: list[dict[str, Any]] = []
            scope_rows: list[dict[str, Any]] = []
            provider: Literal["vision", "structured"] = "vision"
            table = _match_table(requirement, tables) if tables else None
            candidate_rows: list[dict[str, Any]] = []
            detail_fields: set[str] = set()
            coverage: dict[str, Any] = {}
            collection_found = False
            if readiness != "ready":
                requirement_scopes[requirement.id] = _scope_descriptor(
                    requirement,
                    required_predicates=required_predicates,
                    applied_filter_state=applied_filter_state,
                    applied_filters=applied_filters,
                    text_query_fields=text_query_fields,
                    rows=[],
                )
                missing.append(requirement.id)
                continue
            if table is None:
                empty_tables = [
                    candidate
                    for candidate in tables
                    if _authoritative_empty_table(candidate)
                ]
                if len(empty_tables) == 1:
                    table = empty_tables[0]
            table_complete = bool(
                table is not None and _table_supports_requirement(requirement, table)
            )
            empty_complete = bool(
                table is not None and _authoritative_empty_table(table)
            )
            if table_complete:
                rows = _structured_rows(requirement, table)
                scope_rows = rows
                rows = _logical_rows(
                    requirement,
                    rows,
                    keep_unknown=_requirement_has_semantic_predicate(requirement),
                )
            elif self.mode == "enhanced" and table is not None and not empty_complete:
                candidate_rows, detail_fields = _partial_structured_rows(requirement, table)
                if not allow_linked_details:
                    candidate_rows, detail_fields = [], set()
                scope_rows = candidate_rows
                candidate_rows = _logical_rows(
                    requirement, candidate_rows, keep_unknown=True
                )
            if table_complete or empty_complete or candidate_rows:
                scope = _scope_descriptor(
                    requirement,
                    required_predicates=required_predicates,
                    applied_filter_state=applied_filter_state,
                    applied_filters=applied_filters,
                    text_query_fields=text_query_fields,
                    rows=scope_rows,
                )
                scope["query_outcome"] = (
                    "empty" if empty_complete else "candidates"
                )
                requirement_scopes[requirement.id] = scope
                if required_predicates and scope["status"] in {"met", "unmet"}:
                    filter_key = (
                        state_scope,
                        requirement.id,
                        _fingerprint(required_predicates),
                    )
                    if scope["status"] == "met":
                        self._visual_filter_states[filter_key] = "confirmed"
                    else:
                        self._visual_filter_states.pop(filter_key, None)
                if rows or (empty_complete and scope["empty_authoritative"]):
                    # Emit for current visible rows (ReAct support).
                    # Previously gated on scope["status"] == "met"; now emit whenever
                    # we have logical rows so ReAct collector can complete and snapshot
                    # what the worker has made visible, even if overall scope_status is
                    # "candidates" or "unknown".
                    provider = "structured"
                    coverage = _structured_coverage(
                        table,
                        requirement,
                        scope=scope,
                        url=url,
                    )
                    if len(rows) != len(scope_rows):
                        source_total = coverage.pop("total_records", None)
                        if source_total is not None:
                            coverage["source_total_records"] = source_total
                    collection_found = True
            detail_state = self._detail_collections.get(detail_key)
            structured_detail = bool(
                detail_state
                and detail_state.detail_fields.intersection(
                    _control_row(requirement, controls)[1]
                )
            )
            if (
                not collection_found
                and not structured_detail
                and allow_linked_details
                and (
                    not candidate_rows
                    or bool(table and table.get("unmapped_visible_content"))
                )
            ):
                extracted = self._vision_extract(
                    requirement,
                    png,
                    query_filters=applied_filters,
                    page_identity={"url": url, "title": title},
                )
                visually_found = bool(extracted.get("found"))
                start_visible = extracted.get("start_visible")
                if extracted.get("clipped_top_record_visible") is True:
                    start_visible = False
                end_visible = bool(extracted.get("end_visible"))
                raw_visual_rows = list(extracted.get("rows") or [])
                empty_state_evidence = str(
                    extracted.get("empty_state_evidence") or ""
                ).strip()
                visible_empty = bool(
                    extracted.get("empty_state_visible")
                    and not raw_visual_rows
                    and end_visible
                    and empty_state_evidence
                )
                visual_scope = extracted.get("scope_satisfied")
                if required_predicates:
                    visual_scope = _visual_scope_state(
                        self._visual_filter_states,
                        (
                            state_scope,
                            requirement.id,
                            _fingerprint(required_predicates),
                        ),
                        extracted,
                        has_result=visually_found or visible_empty,
                    )
                scope = _scope_descriptor(
                    requirement,
                    required_predicates=required_predicates,
                    applied_filter_state=applied_filter_state,
                    applied_filters=applied_filters,
                    text_query_fields=text_query_fields,
                    rows=raw_visual_rows,
                    visual_scope_satisfied=(
                        visual_scope if isinstance(visual_scope, bool) else None
                    ),
                )
                scope["query_outcome"] = (
                    "empty" if visible_empty else "candidates" if visually_found else "unknown"
                )
                if visual_scope is True and scope["empty_authoritative"]:
                    supplied = _exact_filter_row_values(
                        requirement, required_predicates
                    )
                    raw_visual_rows = [
                        {**row, **supplied} if isinstance(row, dict) else row
                        for row in raw_visual_rows
                    ]
                authoritative_visual_empty = bool(
                    visible_empty and scope["empty_authoritative"]
                )
                requirement_scopes[requirement.id] = scope
                if visually_found or visible_empty:
                    valid_visual_rows = _normalize_visual_rows(
                        requirement,
                        raw_visual_rows,
                    )
                    rows = _logical_rows(
                        requirement,
                        valid_visual_rows,
                        keep_unknown=_requirement_has_semantic_predicate(requirement),
                    )
                    if len(raw_visual_rows) != len(valid_visual_rows):
                        scope["collection_blockers"] = [
                            "visible rows did not satisfy the required row schema"
                        ]
                        scope["schema_rejected_rows"] = (
                            len(raw_visual_rows) - len(valid_visual_rows)
                        )
                    if len(rows) != len(valid_visual_rows):
                        scope.setdefault("collection_blockers", []).append(
                            "visible rows did not satisfy the immutable logical filters"
                        )
                        scope["filter_rejected_rows"] = len(valid_visual_rows) - len(rows)
                    surface = (
                        _structured_coverage(
                            table, requirement, scope=scope, url=url
                        )
                        if table is not None and rows
                        else {}
                    )
                    surface_complete = bool(
                        surface
                        and (
                            table.get("in_viewport") is True
                            or table.get("viewport_pos") == "in"
                        )
                        and len(table.get("rows") or []) == len(rows)
                        and surface.get("at_end") is True
                        and surface.get("partial") is False
                    )
                    movement = dict(surface.get("movement") or {})
                    surface_has_more = bool(
                        surface
                        and (
                            surface.get("has_next_page") is True
                            or _page_has_more(
                                surface.get("page_index"),
                                surface.get("page_count"),
                            )
                            or movement.get("can_scroll_more") is True
                            or movement.get("at_scroll_end") is False
                        )
                    )
                    at_end = bool(
                        (authoritative_visual_empty or surface_complete)
                        and not surface_has_more
                    )
                    coverage = {
                        "scope": requirement.scope,
                        "cardinality": requirement.cardinality,
                        "scope_status": scope["status"],
                        "requested_filters": scope["requested_filters"],
                        "applied_filters": scope["applied_filters"],
                        "collection_key": _visual_collection_key(requirement, scope),
                        "source_scope": "visual_viewport",
                        "window_context": _fingerprint({
                            "route": url,
                            "surface": _surface_marker(table),
                            "window": surface.get("window_key") if surface else "",
                        }),
                        "start_visible": start_visible,
                        "end_visible": end_visible,
                        "at_end": at_end,
                        "partial": not at_end,
                    }
                    if surface:
                        coverage.update({
                            key: surface.get(key)
                            for key in (
                                "total_records",
                                "page_index",
                                "page_count",
                                "has_next_page",
                                "traversal_type",
                                "movement",
                            )
                            if surface.get(key) is not None
                        })
                    if authoritative_visual_empty:
                        coverage.update({
                            "total_records": 0,
                            "coverage_evidence": "explicit_visual_empty_state",
                            "empty_state_evidence": empty_state_evidence,
                        })
                    if surface_complete:
                        coverage.update({
                            "coverage_evidence": "structured_surface_cardinality",
                        })
                    collection_found = bool(rows or authoritative_visual_empty)
                    # Collectors are pure ReAct: emit current visible rows whenever extracted.
                    # LLM will decide completion; no FSM gating.
            requirement_scopes.setdefault(
                requirement.id,
                _scope_descriptor(
                    requirement,
                    required_predicates=required_predicates,
                    applied_filter_state=applied_filter_state,
                    applied_filters=applied_filters,
                    text_query_fields=text_query_fields,
                    rows=[],
                ),
            )
            scope = requirement_scopes[requirement.id]
            requested_fingerprint = json.dumps(
                scope.get("requested_filters") or {},
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
            if candidate_rows and collection_found and provider == "vision":
                # Unlabelled structured text was successfully transcribed on the
                # same list surface; no linked-detail branch remains to resolve.
                self._detail_collections.pop(detail_key, None)
                candidate_rows, detail_fields = [], set()
            assembled = (
                self._assemble_detail_collection(
                    state_key=detail_key,
                    requirement=requirement,
                    candidate_rows=candidate_rows,
                    detail_fields=detail_fields,
                    controls=controls,
                    structured_rows=rows if table_complete else [],
                    scope_status=str(scope.get("status") or "unknown"),
                    observed_rows=(
                        rows
                        if rows and not candidate_rows and provider == "vision"
                        else None
                    ),
                    surface=_fingerprint(_surface_marker(table)),
                    location=str((table or {}).get("location") or ""),
                    scope_key=requested_fingerprint,
                )
                if self.mode == "enhanced"
                else None
            )
            totals_key = (state_scope, requirement.id, requested_fingerprint)
            table_total = table.get("total_records") if table is not None else None
            if table_total not in (None, ""):  # ReAct: capture totals from surfaces even if scope not "met" yet
                try:
                    expected_totals[totals_key] = max(0, int(table_total))
                except (TypeError, ValueError):
                    pass
            expected_total = expected_totals.get(totals_key)
            if assembled is not None:
                detail_state, assembled_rows, detail_progress = assembled
                ready = bool(assembled_rows)
                enough = (
                    expected_total is None
                    or int(detail_progress["candidate_records"]) >= int(expected_total)
                )
                ready_to_complete = ready and enough
                pending_ordinal = detail_state.pending_index
                pending_ordinal = pending_ordinal + 1 if pending_ordinal is not None else None
                current_detail_fields = set(
                    detail_progress.get("current_observed_detail_fields") or []
                )
                scope["detail_resolution"] = {
                    "status": "resolved" if ready_to_complete else "active",
                    **detail_progress,
                    "detail_fields": sorted(detail_state.detail_fields),
                    "pending_candidate_ordinal": pending_ordinal,
                }
                if (controls or table_complete) and (
                    current_detail_fields or pending_ordinal is not None or ready
                ):
                    # Detail evidence arriving through form controls or a
                    # structured detail table is not a collection chunk; a
                    # purely visual detail surface (no controls, no table)
                    # must keep chunk-accumulating its transcribed rows.
                    rows = []
                    collection_found = False
                if (
                    not ready_to_complete
                    and detail_progress.get("next_unresolved_candidate") is None
                    and expected_total is not None
                    and int(detail_progress["candidate_records"]) < int(expected_total)
                ):
                    # Every observed candidate is resolved but the known total is
                    # larger: the worker must see the deficit plainly instead of
                    # concluding the collection is finished (fake-complete loop).
                    scope["detail_resolution"]["window_exhausted"] = True
                    scope["detail_resolution"]["known_total"] = int(expected_total)
                if ready_to_complete:  # ReAct: no scope["status"]=="met" gate for collector detail rows
                    existing = self.data_store.collection_for_requirement(requirement.id)
                    if existing is not None and (
                        existing.coverage.get("status") != "complete"
                        or existing.row_count == 0
                    ):
                        self.data_store.discard_requirement(requirement.id)
                        existing = None
                    if existing is None:
                        rows = assembled_rows
                        provider = "structured"
                        coverage = {
                            "scope": requirement.scope,
                            "scope_status": str(scope.get("status") or "unknown"),
                            "requested_filters": scope["requested_filters"],
                            "applied_filters": scope["applied_filters"],
                            "collection_key": _visual_collection_key(requirement, scope),
                            "source_scope": "linked_detail",
                            "window_context": requirement.id,
                            "at_end": True,
                            "partial": False,
                            "total_records": detail_progress["candidate_records"],
                            "coverage_evidence": "linked_detail_assembly",
                            **detail_progress,
                        }
                        collection_found = True
            if provider == "vision" and coverage and expected_total is not None:
                # A list surface may reveal the candidate cardinality while the
                # required fields live on linked detail surfaces. Carry that
                # cardinality across navigation so each detail is a chunk of one
                # collection, rather than a falsely complete one-row collection.
                coverage["total_records"] = expected_total
            if not collection_found:
                accumulated = self.data_store.collection_for_requirement(
                    requirement.id
                )
                if (
                    accumulated is not None
                    and accumulated.row_schema == requirement.row_schema
                    and json.dumps(
                        accumulated.coverage.get("requested_filters") or {},
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    )
                    == requested_fingerprint
                ):
                    # Pure ReAct collectors: retain prior accumulated collection across frames.
                    # The worker owns the collection; a single page's scope_status does not reset it.
                    # This keeps chunks visible while the worker navigates between list and detail surfaces.
                    collections.append(accumulated)
                    continue
                missing.append(requirement.id)
                continue
            if requirement.field_types:
                coverage["normalization"] = dict(requirement.field_types)
                coverage["normalized_rows"] = len(rows)
                coverage["rejected_rows"] = 0
            coverage["requested"] = requirement.coverage
            chunk, collection, _created = self.data_store.put_chunk(
                requirement_id=requirement.id,
                frame_id=frame_id,
                provider=provider,
                rows=rows,
                row_schema=requirement.row_schema,
                coverage=coverage,
            )
            chunks.append(chunk)
            collections.append(collection)

        materialized = MaterializedFrame(
            frame_id=frame_id,
            screenshot_path=str(screenshot_path),
            readiness=readiness,
            readiness_reason=readiness_reason,
            platform_time=(
                getattr(self, "platform_time", None)
                or host_time_fallback(
                    "browser",
                    reason="legacy materializer has no frozen platform clock",
                )
            ).model_dump(mode="json"),
            url=url or "",
            title=title or "",
            controls=controls,
            visible_collection_regions=visible_collection_regions,
            structured_surfaces=[
                _structured_surface_descriptor(table)
                for table in tables
                if isinstance(table, dict)
            ],
            applied_filters=applied_filters,
            requirement_scopes=requirement_scopes,
            chunks=chunks,
            collections=collections,
            missing_requirements=missing,
        )
        return materialized, png

    def _reader_config(self) -> Any:
        return getattr(self, "_screen_reader_cfg", None) or getattr(
            self, "_vision_cfg", None
        )

    def _reader_invoke(self, messages: list[Any]) -> Any:
        """Invoke the dedicated screen-reader model, falling back to the perception model."""
        cfg = self._reader_config()
        model = getattr(cfg, "model", None) or self.model
        reader = getattr(self, "_screen_reader", None) or getattr(
            self, "_vision", None
        )
        return reader.bind(
            response_format={"type": "json_object"},
            **chat_request_kwargs(model),
        ).invoke(messages)

    def _screen_read(
        self,
        requirement: DataRequirement,
        png: bytes,
        *,
        query_filters: dict[str, Any],
        page_identity: dict[str, str] | None = None,
    ) -> tuple[dict[str, Any], list[Any], Any, float]:
        """Pure transcription of every visible record — no semantic judgment.

        A dedicated screen-reader model (cheap flash) reads all records that fit
        the row schema, matching or not. Matching is a separate text step, so a
        paraphrase the judge accepts is never dropped at transcription time.
        """
        prompt = (
            f"Data requirement: {requirement.description}\n"
            f"Visible target label: {requirement.target_label or '(not specified)'}\n"
            f"Current page identity: {json.dumps(page_identity or {}, ensure_ascii=False)}\n"
            f"Current UI query state: {json.dumps(query_filters, ensure_ascii=False)}\n"
            f"Visible field labels: {json.dumps(requirement.field_sources, ensure_ascii=False)}\n"
            "Visible row JSON Schema (transcribe EVERY visible record, matching or not): "
            f"{json.dumps(_visible_row_schema(requirement, {}), ensure_ascii=False)}"
        )
        started_at = time.perf_counter()
        cfg = self._reader_config()
        messages = [
            SystemMessage(content=_SCREEN_READER_SYSTEM),
            image_message(prompt, png, scale=float(getattr(cfg, "image_scale", 1.0))),
        ]
        response = self._reader_invoke(messages)
        value = parse_json_object(response.content)
        if not isinstance(value.get("rows"), list):
            value["rows"] = []
        return value, messages, response, time.perf_counter() - started_at

    def _semantic_judge(
        self,
        requirement: DataRequirement,
        rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Text-only filter of transcribed rows by a semantic predicate.

        The screen reader transcribes everything; this judge keeps the rows whose
        content field mentions or describes the phrase, paraphrases included —
        the exact judgment the fused transcribe-and-filter perception kept missing
        (e.g. "half my ear into it" for small ear cups).
        """
        predicates = display_filter_predicates(requirement.filters)
        prompt = (
            "Transcribed records are below. Keep the subset that satisfies this semantic "
            f"predicate: {json.dumps(predicates, ensure_ascii=False)}\n"
            "A record matches when its text mentions or describes the phrase, including "
            "paraphrases; exclude a record only when it visibly does not relate.\n"
            'Return ONLY a JSON object with the 0-based indices of the kept records: '
            '{"matched_indices": [0, 2, ...]}\n'
            f"Records:\n{json.dumps(rows, ensure_ascii=False)}"
        )
        response = self._reader_invoke([HumanMessage(content=prompt)])
        indices = parse_json_object(response.content).get("matched_indices")
        if not isinstance(indices, list):
            return []  # undecided judge → keep nothing; a later frame re-reads
        return [rows[i] for i in indices if isinstance(i, int) and 0 <= i < len(rows)]

    def _vision_extract(
        self,
        requirement: DataRequirement,
        png: bytes,
        *,
        query_filters: dict[str, Any],
        page_identity: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        value, messages, response, llm_elapsed_s = self._screen_read(
            requirement,
            png,
            query_filters=query_filters,
            page_identity=page_identity,
        )
        if _requirement_has_semantic_predicate(requirement):
            rows = value.get("rows") or []
            if rows:
                value["rows"] = self._semantic_judge(requirement, rows)
            value["found"] = bool(value["rows"])
        # Preserve visibly transcribed rows until observe() classifies them. If
        # schema validation drops a non-empty row here, the caller can no longer
        # distinguish an incomplete record from an authoritative empty surface.
        raw_rows = list(value["rows"])
        valid_rows = _normalize_visual_rows(requirement, raw_rows)
        on_event = getattr(self, "_on_event", None)
        if on_event is not None:
            on_event(
                "perception_extract",
                requirement_id=requirement.id,
                found=bool(value.get("found")),
                row_count=len(valid_rows),
                observed_row_count=len(raw_rows),
                schema_rejected_rows=len(raw_rows) - len(valid_rows),
                empty_state_visible=bool(value.get("empty_state_visible")),
                empty_state_evidence=str(
                    value.get("empty_state_evidence") or ""
                ).strip(),
                end_visible=bool(value.get("end_visible")),
                llm_elapsed_s=round(llm_elapsed_s, 3),
                token_usage=response_usage(response),
                context_reports=diagnostic_prompt_reports(
                    "tool_agent.perception",
                    messages,
                    response,
                    parsed=value,
                    schema="Visual collection extraction",
                ),
            )
        return value
