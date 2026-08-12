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
from langchain_core.messages import SystemMessage
from langchain_openai import ChatOpenAI

from gui_agent.core.config import resolve_llm_config
from gui_agent.core.runtime.clock import PlatformTimeSnapshot, host_time_fallback
from gui_agent.core.tool_agent.filter_state import (
    canonical_filter_value,
    compile_filter_predicates,
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

PerceptionMode = Literal["vision-only", "enhanced"]

_VISION_SYSTEM = load_prompt_text("task.tool_agent.visual_transcription")


class DataNormalizationError(ValueError):
    """A declared runtime field type could not be normalized losslessly."""


@dataclass
class _DetailCollectionState:
    """Private partial rows assembled across one list/detail traversal."""

    rows: list[dict[str, Any]]
    detail_fields: set[str]
    pending_index: int | None = None


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


def _table_fields(table: dict[str, Any]) -> set[str]:
    available = {
        _normalize(str(header)) for header in list(table.get("headers") or [])
    }
    for row in list(table.get("rows") or []):
        if isinstance(row, dict):
            available.update(_normalize(str(key)) for key in row)
    return available


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


def _table_supports_requirement(
    requirement: DataRequirement,
    table: dict[str, Any],
) -> bool:
    required_sources = {
        _normalize(requirement.field_sources.get(field, field))
        for field in (requirement.row_schema.get("properties") or {})
    }
    return bool(required_sources) and required_sources.issubset(_table_fields(table))


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
    required_sources = {
        _normalize(requirement.field_sources.get(field, field))
        for field in (requirement.row_schema.get("properties") or {})
    }
    ranked: list[tuple[tuple[bool, bool, int, int], dict[str, Any]]] = []
    for table in tables:
        caption = _normalize(str(table.get("caption") or ""))
        caption_words = _words(str(table.get("caption") or ""))
        available = _table_fields(table)
        field_matches = len(required_sources.intersection(available))
        rank = (
            bool(target and (target == caption or target in caption or caption in target)),
            bool(required_sources) and field_matches == len(required_sources),
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
    rows: list[dict[str, Any]] = []
    for row_index, source_row in enumerate(list(table.get("rows") or []), start=1):
        if not isinstance(source_row, dict):
            continue
        normalized_sources = {_normalize(str(key)): value for key, value in source_row.items()}
        output: dict[str, Any] = {}
        for field, field_schema in properties.items():
            source = source_map.get(field, field)
            value = source_row.get(source, normalized_sources.get(_normalize(source)))
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
    available = _table_fields(table)
    list_fields = {
        field
        for field in properties
        if _normalize(requirement.field_sources.get(field, field)) in available
    }
    if len(list_fields) < 2:
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
    for row in rows:
        for field, predicate in predicates.items():
            # Requirement filter keys are normalized row-schema fields. The filter
            # compiler only canonicalizes their spelling for predicate matching.
            row_field = next(
                (
                    key
                    for key in active_filters
                    if key.replace("_", " ").casefold() == field
                ),
                field.replace(" ", "_"),
            )
            if row_field not in row:
                return None
            value = row[row_field]
            field_type = requirement.field_types.get(row_field)
            field_name = requirement.field_sources.get(row_field, row_field)
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
    return True


def _scope_descriptor(
    requirement: DataRequirement,
    *,
    acquisition_filters: dict[str, Any],
    applied_filter_state: Any,
    applied_filters: dict[str, Any],
    rows: list[dict[str, Any]],
    visual_scope_satisfied: bool | None = None,
) -> dict[str, Any]:
    requested_ui_filters = {
        requirement.field_sources.get(field, field): value
        for field, value in acquisition_filters.items()
    }
    if not requested_ui_filters:
        has_applied_filters = bool(applied_filters)
        return {
            "status": "unmet" if has_applied_filters else "met",
            "requested_filters": {},
            "applied_filters": dict(applied_filters),
            "evidence": (
                "unexpected_applied_filters"
                if has_applied_filters
                else "no_filter_required"
            ),
        }
    requested = compile_filter_predicates(requested_ui_filters)
    if applied_filter_state is not None:
        match = match_filter_state(requested, applied_filter_state)
        if match in {"met", "unmet"}:
            return {
                "status": match,
                "requested_filters": requested_ui_filters,
                "applied_filters": dict(applied_filters),
                "evidence": "applied_filter_state",
            }
    if visual_scope_satisfied is not None:
        return {
            "status": "met" if visual_scope_satisfied else "unmet",
            "requested_filters": requested_ui_filters,
            "applied_filters": dict(applied_filters),
            "evidence": "visual_filter_state",
        }
    row_match = _rows_satisfy_filters(
        requirement,
        rows,
        filters=acquisition_filters,
    )
    return {
        "status": "met" if row_match is True else "unmet" if row_match is False else "unknown",
        "requested_filters": requested_ui_filters,
        "applied_filters": dict(applied_filters),
        "evidence": "row_values" if row_match is not None else "unavailable",
    }


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()[:16]


def _surface_marker(table: dict[str, Any] | None) -> Any:
    return [] if table is None else (
        table.get("path") or table.get("caption") or table.get("headers") or []
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
    }


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
        self._on_event = on_event
        self._expected_totals: dict[tuple[str, str], int] = {}
        self._detail_collections: dict[str, _DetailCollectionState] = {}
        cfg = resolve_llm_config("tool_agent.perception")
        self.model = cfg.model
        self._vision = ChatOpenAI(
            model=cfg.model,
            api_key=cfg.api_key,
            base_url=cfg.base_url,
            timeout=cfg.timeout_s,
            max_retries=cfg.max_retries,
            temperature=0,
        )

    def _assemble_detail_collection(
        self,
        *,
        requirement: DataRequirement,
        candidate_rows: list[dict[str, Any]],
        detail_fields: set[str],
        controls: list[dict[str, Any]],
        scope_status: str,
    ) -> tuple[_DetailCollectionState, list[dict[str, Any]], dict[str, Any]] | None:
        """Accumulate list candidates and form details without exposing row values."""

        state = self._detail_collections.get(requirement.id)
        if candidate_rows and detail_fields and scope_status == "met":
            identity_fields = detail_fields | (state.detail_fields if state else set())
            def identities(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
                return [
                    {key: value for key, value in row.items() if key not in identity_fields}
                    for row in items
                ]

            if state is None or identities(state.rows) != identities(candidate_rows):
                state = _DetailCollectionState(
                    rows=[dict(row) for row in candidate_rows],
                    detail_fields=set(detail_fields),
                )
                self._detail_collections[requirement.id] = state
            else:
                state.detail_fields.update(detail_fields)
                for row, candidate in zip(state.rows, candidate_rows, strict=True):
                    row.update(candidate)
        if state is None or not state.rows or not state.detail_fields:
            return None

        detail, observed = _control_row(requirement, controls)
        if observed.intersection(state.detail_fields):
            scores = [
                sum(
                    key not in state.detail_fields
                    and _nonempty(value)
                    and row.get(key) == value
                    for key, value in detail.items()
                )
                for row in state.rows
            ]
            best = max(scores, default=0)
            matching = scores.index(best) if best and scores.count(best) == 1 else None
            target = matching if matching is not None else state.pending_index
            if target is not None:
                for key in state.detail_fields.intersection(observed):
                    value = detail.get(key)
                    if matching is not None or (
                        _nonempty(value) and not _nonempty(state.rows[target].get(key))
                    ):
                        state.rows[target][key] = value
                state.pending_index = target if any(
                    not _nonempty(state.rows[target].get(field))
                    for field in state.detail_fields
                ) else None

        unresolved_indexes = [
            index for index, row in enumerate(state.rows)
            if any(not _nonempty(row.get(field)) for field in state.detail_fields)
        ]
        progress = {
            "candidate_records": len(state.rows),
            "current_observed_detail_fields": sorted(
                state.detail_fields.intersection(observed)
            ),
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
        acquisition_filters: dict[str, Any] | None = None,
        frame_no: int,
    ) -> tuple[MaterializedFrame, bytes]:
        frame_id = f"frame:{frame_no}"
        screenshot_path = self.log_dir / f"screenshot_tool_agent_{frame_no}.png"
        tables: list[dict[str, Any]] = []
        controls: list[dict[str, Any]] = []
        applied_filters: dict[str, Any] = {}
        applied_filter_state = None
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
            url, title = observation.url or "", observation.title or ""
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
        for requirement in requirements:
            attempt_filters = (
                dict(acquisition_filters)
                if acquisition_filters is not None and len(requirements) == 1
                else dict(requirement.filters)
            )
            rows: list[dict[str, Any]] = []
            provider: Literal["vision", "structured"] = "vision"
            table = _match_table(requirement, tables) if tables else None
            candidate_rows: list[dict[str, Any]] = []
            detail_fields: set[str] = set()
            coverage: dict[str, Any] = {}
            collection_found = False
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
            elif self.mode == "enhanced" and table is not None and not empty_complete:
                candidate_rows, detail_fields = _partial_structured_rows(requirement, table)
            if table_complete or empty_complete or candidate_rows:
                scope = _scope_descriptor(
                    requirement,
                    acquisition_filters=attempt_filters,
                    applied_filter_state=applied_filter_state,
                    applied_filters=applied_filters,
                    rows=rows or candidate_rows,
                )
                requirement_scopes[requirement.id] = scope
                if scope["status"] == "met" and (table_complete or empty_complete):
                    provider = "structured"
                    coverage = _structured_coverage(
                        table,
                        requirement,
                        scope=scope,
                        url=url,
                    )
                    collection_found = True
            detail_state = self._detail_collections.get(requirement.id)
            structured_detail = bool(
                detail_state
                and detail_state.detail_fields.intersection(
                    _control_row(requirement, controls)[1]
                )
            )
            if not collection_found and not candidate_rows and not structured_detail:
                extracted = self._vision_extract(
                    requirement,
                    png,
                    acquisition_filters=attempt_filters,
                )
                visually_found = bool(extracted.get("found"))
                end_visible = bool(extracted.get("end_visible"))
                visual_scope = extracted.get("scope_satisfied")
                scope = _scope_descriptor(
                    requirement,
                    acquisition_filters=attempt_filters,
                    applied_filter_state=applied_filter_state,
                    applied_filters=applied_filters,
                    rows=list(extracted.get("rows") or []),
                    visual_scope_satisfied=(
                        visual_scope if isinstance(visual_scope, bool) else None
                    ),
                )
                requirement_scopes[requirement.id] = scope
                if visually_found:
                    raw_visual_rows = list(extracted.get("rows") or [])
                    rows = _normalize_visual_rows(
                        requirement,
                        raw_visual_rows,
                    )
                    rejected_visual_rows = len(raw_visual_rows) - len(rows)
                    if rejected_visual_rows:
                        scope["collection_blockers"] = [
                            "visible rows did not satisfy the required row schema"
                        ]
                        scope["schema_rejected_rows"] = rejected_visual_rows
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
                    coverage = {
                        "scope": requirement.scope,
                        "scope_status": scope["status"],
                        "requested_filters": scope["requested_filters"],
                        "applied_filters": scope["applied_filters"],
                        "collection_key": "visual:" + _fingerprint({
                            "requirement": requirement.id,
                            "filters": scope["requested_filters"],
                        }),
                        "source_scope": "visual_viewport",
                        "window_context": _fingerprint({
                            "route": url,
                            "surface": _surface_marker(table),
                        }),
                        "end_visible": end_visible,
                        "at_end": end_visible or surface_complete,
                        "partial": not (end_visible or surface_complete),
                    }
                    if surface_complete:
                        coverage.update({
                            "total_records": len(rows),
                            "traversal_type": surface.get("traversal_type"),
                            "movement": surface.get("movement") or {},
                            "coverage_evidence": "structured_surface_cardinality",
                        })
                    collection_found = bool(
                        scope["status"] == "met"
                        and (
                            rows
                            or visually_found
                            and end_visible
                            and not raw_visual_rows
                        )
                    )
            requirement_scopes.setdefault(
                requirement.id,
                _scope_descriptor(
                    requirement,
                    acquisition_filters=attempt_filters,
                    applied_filter_state=applied_filter_state,
                    applied_filters=applied_filters,
                    rows=[],
                ),
            )
            scope = requirement_scopes[requirement.id]
            assembled = (
                self._assemble_detail_collection(
                    requirement=requirement,
                    candidate_rows=candidate_rows,
                    detail_fields=detail_fields,
                    controls=controls,
                    scope_status=str(scope.get("status") or "unknown"),
                )
                if self.mode == "enhanced"
                else None
            )
            if assembled is not None:
                detail_state, assembled_rows, detail_progress = assembled
                ready = bool(assembled_rows)
                pending_ordinal = detail_state.pending_index
                pending_ordinal = pending_ordinal + 1 if pending_ordinal is not None else None
                current_detail_fields = set(
                    detail_progress.get("current_observed_detail_fields") or []
                )
                scope["detail_resolution"] = {
                    "status": "resolved" if ready else "active",
                    **detail_progress,
                    "detail_fields": sorted(detail_state.detail_fields),
                    "pending_candidate_ordinal": pending_ordinal,
                }
                if current_detail_fields or pending_ordinal is not None or ready:
                    rows = []
                    collection_found = False
                if scope["status"] == "met" and ready:
                    rows = assembled_rows
                    provider = "structured"
                    coverage = {
                        "scope": requirement.scope,
                        "scope_status": "met",
                        "requested_filters": scope["requested_filters"],
                        "applied_filters": scope["applied_filters"],
                        "collection_key": "visual:" + _fingerprint({
                            "requirement": requirement.id,
                            "filters": scope["requested_filters"],
                        }),
                        "source_scope": "linked_detail",
                        "window_context": requirement.id,
                        "at_end": True,
                        "partial": False,
                        "total_records": detail_progress["candidate_records"],
                        "coverage_evidence": "linked_detail_assembly",
                        **detail_progress,
                    }
                    collection_found = True
            scope_key = (
                requirement.id,
                json.dumps(
                    scope.get("requested_filters") or {},
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ),
            )
            table_total = table.get("total_records") if table is not None else None
            if scope["status"] == "met" and table_total not in (None, ""):
                try:
                    expected_totals[scope_key] = max(0, int(table_total))
                except (TypeError, ValueError):
                    pass
            expected_total = expected_totals.get(scope_key)
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
                    and scope["status"] == "met"
                    and accumulated.row_schema == requirement.row_schema
                    and json.dumps(
                        accumulated.coverage.get("requested_filters") or {},
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    )
                    == scope_key[1]
                ):
                    # Collection state belongs to the Worker, not to one page.
                    # Keep accumulated descriptors visible while the Worker moves
                    # between a candidate list and linked detail surfaces.
                    collections.append(accumulated)
                    continue
                missing.append(requirement.id)
                continue
            if requirement.field_types:
                coverage["normalization"] = dict(requirement.field_types)
                coverage["normalized_rows"] = len(rows)
                coverage["rejected_rows"] = 0
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

    def _vision_extract(
        self,
        requirement: DataRequirement,
        png: bytes,
        *,
        acquisition_filters: dict[str, Any],
    ) -> dict[str, Any]:
        prompt = (
            f"Data requirement: {requirement.description}\n"
            f"Visible target label: {requirement.target_label or '(not specified)'}\n"
            "Task reference time (frozen platform clock): "
            f"{json.dumps((getattr(self, 'platform_time', None) or host_time_fallback('browser', reason='legacy perception fallback')).model_dump(mode='json'), ensure_ascii=False)}\n"
            f"Logical row filters: {json.dumps(requirement.filters, ensure_ascii=False)}\n"
            f"Current UI acquisition filters: "
            f"{json.dumps(acquisition_filters, ensure_ascii=False)}\n"
            f"Required row JSON Schema: {json.dumps(requirement.row_schema, ensure_ascii=False)}"
        )
        started_at = time.perf_counter()
        messages = [SystemMessage(content=_VISION_SYSTEM), image_message(prompt, png)]
        response = self._vision.bind(
            response_format={"type": "json_object"},
            extra_body={"enable_thinking": False},
        ).invoke(messages)
        llm_elapsed_s = time.perf_counter() - started_at
        value = parse_json_object(response.content)
        rows = value.get("rows")
        if not isinstance(rows, list):
            value["rows"] = []
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
