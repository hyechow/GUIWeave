"""Automatic perception materialization for vision-only and enhanced modes."""

from __future__ import annotations

import json
import hashlib
import re
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Literal

from jsonschema import ValidationError, validate
from langchain_core.messages import SystemMessage
from langchain_openai import ChatOpenAI

from gui_agent.core.config import resolve_llm_config
from gui_agent.core.filter_contract import (
    canonical_filter_value,
    compile_filter_predicates,
    match_filter_state,
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


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.casefold())


def _table_fields(table: dict[str, Any]) -> set[str]:
    available = {
        _normalize(str(header)) for header in list(table.get("headers") or [])
    }
    for row in list(table.get("rows") or []):
        if isinstance(row, dict):
            available.update(_normalize(str(key)) for key in row)
    return available


def _table_supports_requirement(
    requirement: DataRequirement,
    table: dict[str, Any],
) -> bool:
    required_sources = {
        _normalize(requirement.field_sources.get(field, field))
        for field in (requirement.row_schema.get("properties") or {})
    }
    return bool(required_sources) and required_sources.issubset(_table_fields(table))


def _match_table(requirement: DataRequirement, tables: list[dict[str, Any]]) -> dict[str, Any] | None:
    wanted = _normalize(requirement.target_label or requirement.description)
    ranked: list[tuple[int, dict[str, Any]]] = []
    for table in tables:
        caption = _normalize(str(table.get("caption") or ""))
        score = 100 if wanted and (wanted == caption or wanted in caption or caption in wanted) else 0
        if score == 0:
            wanted_words = set(re.findall(r"[a-z0-9]+", (requirement.target_label or requirement.description).casefold()))
            caption_words = set(re.findall(r"[a-z0-9]+", str(table.get("caption") or "").casefold()))
            score = len(wanted_words.intersection(caption_words))
        available = _table_fields(table)
        required_sources = {
            _normalize(requirement.field_sources.get(field, field))
            for field in (requirement.row_schema.get("properties") or {})
        }
        field_matches = len(required_sources.intersection(available))
        if required_sources and field_matches == len(required_sources):
            score += 50 + field_matches
        elif required_sources:
            score -= 50
        ranked.append((score, table))
    if not ranked:
        return None
    score, table = max(ranked, key=lambda item: item[0])
    return table if score > 0 else None


def _structured_rows(
    requirement: DataRequirement,
    table: dict[str, Any],
) -> list[dict[str, Any]]:
    source_map = requirement.field_sources
    properties = requirement.row_schema.get("properties") or {}
    rows: list[dict[str, Any]] = []
    for source_row in list(table.get("rows") or []):
        if not isinstance(source_row, dict):
            continue
        normalized_sources = {_normalize(str(key)): value for key, value in source_row.items()}
        output: dict[str, Any] = {}
        for field, field_schema in properties.items():
            source = source_map.get(field, field)
            value = source_row.get(source, normalized_sources.get(_normalize(source)))
            field_type = field_schema.get("type") if isinstance(field_schema, dict) else None
            if value is not None and field_type in {"integer", "number"}:
                try:
                    value = int(str(value).replace(",", "")) if field_type == "integer" else float(str(value).replace(",", ""))
                except ValueError:
                    pass
            output[field] = value
        try:
            validate(instance=output, schema=requirement.row_schema)
        except ValidationError:
            continue
        rows.append(output)
    return rows


def _compare_values(left: Any, right: Any) -> int:
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
) -> bool | None:
    if not requirement.filters:
        return True
    if not rows:
        return None
    predicates = compile_filter_predicates(requirement.filters)
    for row in rows:
        for field, predicate in predicates.items():
            # Requirement filter keys are normalized row-schema fields. The filter
            # compiler only canonicalizes their spelling for predicate matching.
            row_field = next(
                (
                    key
                    for key in requirement.filters
                    if key.replace("_", " ").casefold() == field
                ),
                field.replace(" ", "_"),
            )
            if row_field not in row:
                return None
            value = row[row_field]
            if predicate.operator == "eq":
                matches = _compare_values(value, predicate.values[0]) == 0
            elif predicate.operator == "gte":
                matches = _compare_values(value, predicate.values[0]) >= 0
            elif predicate.operator == "lte":
                matches = _compare_values(value, predicate.values[0]) <= 0
            else:
                matches = (
                    _compare_values(value, predicate.values[0]) >= 0
                    and _compare_values(value, predicate.values[1]) <= 0
                )
            if not matches:
                return False
    return True


def _scope_descriptor(
    requirement: DataRequirement,
    *,
    applied_filter_state: Any,
    applied_filters: dict[str, Any],
    rows: list[dict[str, Any]],
    visual_scope_satisfied: bool | None = None,
) -> dict[str, Any]:
    requested_ui_filters = {
        requirement.field_sources.get(field, field): value
        for field, value in requirement.filters.items()
    }
    if not requested_ui_filters:
        return {
            "status": "met",
            "requested_filters": {},
            "applied_filters": dict(applied_filters),
            "evidence": "no_filter_required",
        }
    requested = compile_filter_predicates(requested_ui_filters)
    if applied_filter_state is not None:
        match = match_filter_state(requested, applied_filter_state)
        if match.status in {"met", "unmet"}:
            return {
                "status": match.status,
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
    row_match = _rows_satisfy_filters(requirement, rows)
    return {
        "status": "met" if row_match is True else "unmet" if row_match is False else "unknown",
        "requested_filters": requested_ui_filters,
        "applied_filters": dict(applied_filters),
        "evidence": "row_values" if row_match is not None else "unavailable",
    }


def _collection_key(
    table: dict[str, Any],
    requirement: DataRequirement,
    scope: dict[str, Any],
    url: str,
) -> str:
    payload = {
        "surface": table.get("path") or table.get("caption") or table.get("headers") or [],
        "route": url,
        "schema": requirement.row_schema,
        "filters": scope.get("applied_filters") or scope.get("requested_filters") or {},
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()[:16]
    return f"surface:{digest}"


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
        on_event: Callable[..., None] | None = None,
    ) -> None:
        self.mode = mode
        self.data_store = data_store
        self.log_dir = log_dir
        self._on_event = on_event
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

    def observe(
        self,
        *,
        bundle: Any,
        platform: Any,
        requirements: list[DataRequirement],
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
            controls = [
                {key: value for key, value in item.items() if key != "rect"}
                for item in list(getattr(observation, "form_controls", None) or [])
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
        requirement_scopes: dict[str, dict[str, Any]] = {}
        for requirement in requirements:
            rows: list[dict[str, Any]] = []
            provider: Literal["vision", "structured"] = "vision"
            table = _match_table(requirement, tables) if tables else None
            coverage: dict[str, Any] = {}
            collection_found = False
            if table is not None and _table_supports_requirement(requirement, table):
                rows = _structured_rows(requirement, table)
                scope = _scope_descriptor(
                    requirement,
                    applied_filter_state=applied_filter_state,
                    applied_filters=applied_filters,
                    rows=rows,
                )
                requirement_scopes[requirement.id] = scope
                if scope["status"] == "met":
                    provider = "structured"
                    coverage = _structured_coverage(
                        table,
                        requirement,
                        scope=scope,
                        url=url,
                    )
                    collection_found = True
            if table is None or not _table_supports_requirement(requirement, table):
                extracted = self._vision_extract(requirement, png)
                visually_found = bool(extracted.get("found"))
                end_visible = bool(extracted.get("end_visible"))
                visual_scope = extracted.get("scope_satisfied")
                scope = _scope_descriptor(
                    requirement,
                    applied_filter_state=applied_filter_state,
                    applied_filters=applied_filters,
                    rows=list(extracted.get("rows") or []),
                    visual_scope_satisfied=(
                        visual_scope if isinstance(visual_scope, bool) else None
                    ),
                )
                requirement_scopes[requirement.id] = scope
                if visually_found:
                    rows = list(extracted.get("rows") or [])
                    coverage = {
                        "scope": requirement.scope,
                        "scope_status": scope["status"],
                        "requested_filters": scope["requested_filters"],
                        "applied_filters": scope["applied_filters"],
                        "collection_key": (
                            "visual:"
                            + hashlib.sha256(
                                json.dumps(
                                    {
                                        "requirement": requirement.id,
                                        "filters": scope["requested_filters"],
                                    },
                                    ensure_ascii=False,
                                    sort_keys=True,
                                ).encode()
                            ).hexdigest()[:16]
                        ),
                        "source_scope": "visual_viewport",
                        "end_visible": end_visible,
                        "at_end": end_visible,
                        "partial": not end_visible,
                    }
                    collection_found = bool(rows) and scope["status"] == "met"
            requirement_scopes.setdefault(
                requirement.id,
                _scope_descriptor(
                    requirement,
                    applied_filter_state=applied_filter_state,
                    applied_filters=applied_filters,
                    rows=[],
                ),
            )
            if not collection_found:
                missing.append(requirement.id)
                continue
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
            url=url or "",
            title=title or "",
            controls=controls,
            applied_filters=applied_filters,
            requirement_scopes=requirement_scopes,
            chunks=chunks,
            collections=collections,
            missing_requirements=missing,
        )
        return materialized, png

    def _vision_extract(self, requirement: DataRequirement, png: bytes) -> dict[str, Any]:
        prompt = (
            f"Data requirement: {requirement.description}\n"
            f"Visible target label: {requirement.target_label or '(not specified)'}\n"
            f"Required UI filter scope: {json.dumps(requirement.filters, ensure_ascii=False)}\n"
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
        valid_rows = []
        for row in value["rows"]:
            try:
                validate(instance=row, schema=requirement.row_schema)
            except ValidationError:
                continue
            valid_rows.append(row)
        value["rows"] = valid_rows
        on_event = getattr(self, "_on_event", None)
        if on_event is not None:
            on_event(
                "perception_extract",
                requirement_id=requirement.id,
                found=bool(value.get("found")),
                row_count=len(valid_rows),
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
