"""Automatic perception materialization for vision-only and enhanced modes."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from jsonschema import ValidationError, validate
from langchain_core.messages import SystemMessage
from langchain_openai import ChatOpenAI

from gui_agent.core.config import resolve_llm_config
from gui_agent.prompts import load_prompt_text
from gui_agent.core.tool_agent.contracts import (
    DataRequirement,
    MaterializedFrame,
)
from gui_agent.core.tool_agent.data_store import RuntimeDataStore
from gui_agent.core.tool_agent.protocol import image_message, parse_json_object

PerceptionMode = Literal["vision-only", "enhanced"]

_VISION_SYSTEM = load_prompt_text("task.tool_agent.visual_transcription")


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.casefold())


def _match_table(requirement: DataRequirement, tables: list[dict[str, Any]]) -> dict[str, Any] | None:
    wanted = _normalize(requirement.target_label or requirement.description)
    ranked: list[tuple[int, dict[str, Any]]] = []
    for table in tables:
        if not (
            table.get("in_viewport") is True
            or table.get("viewport_pos") == "in"
        ):
            continue
        caption = _normalize(str(table.get("caption") or ""))
        if not caption:
            continue
        score = 100 if wanted and (wanted == caption or wanted in caption or caption in wanted) else 0
        if score == 0:
            wanted_words = set(re.findall(r"[a-z0-9]+", (requirement.target_label or requirement.description).casefold()))
            caption_words = set(re.findall(r"[a-z0-9]+", str(table.get("caption") or "").casefold()))
            score = len(wanted_words.intersection(caption_words))
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


class PerceptionMaterializer:
    def __init__(
        self,
        *,
        mode: PerceptionMode,
        data_store: RuntimeDataStore,
        log_dir: Path,
    ) -> None:
        self.mode = mode
        self.data_store = data_store
        self.log_dir = log_dir
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
        if self.mode == "enhanced":
            observation = bundle.make_perception(platform, screenshot_path).observe()
            png = observation.png_bytes
            tables = list(observation.tables or [])
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
        for requirement in requirements:
            rows: list[dict[str, Any]] = []
            provider: Literal["vision", "structured"] = "vision"
            end_visible = False
            table = _match_table(requirement, tables) if tables else None
            extracted = self._vision_extract(requirement, png)
            visually_found = bool(extracted.get("found"))
            end_visible = bool(extracted.get("end_visible"))
            # ``in_viewport`` means that some part of a DOM table intersects the viewport;
            # it does not prove that all returned DOM rows are visible.  Screenshot coverage
            # is therefore the hard gate before enhanced mode may materialize the full table.
            if table is not None and visually_found and end_visible:
                rows = _structured_rows(requirement, table)
                provider = "structured"
            elif visually_found:
                rows = list(extracted.get("rows") or [])
            if not rows:
                missing.append(requirement.id)
                continue
            chunk, collection, _created = self.data_store.put_chunk(
                requirement_id=requirement.id,
                frame_id=frame_id,
                provider=provider,
                rows=rows,
                row_schema=requirement.row_schema,
                coverage={"end_visible": end_visible, "scope": requirement.scope},
            )
            chunks.append(chunk)
            collections.append(collection)

        materialized = MaterializedFrame(
            frame_id=frame_id,
            screenshot_path=str(screenshot_path),
            url=url or "",
            title=title or "",
            chunks=chunks,
            collections=collections,
            missing_requirements=missing,
        )
        return materialized, png

    def _vision_extract(self, requirement: DataRequirement, png: bytes) -> dict[str, Any]:
        prompt = (
            f"Data requirement: {requirement.description}\n"
            f"Visible target label: {requirement.target_label or '(not specified)'}\n"
            f"Required row JSON Schema: {json.dumps(requirement.row_schema, ensure_ascii=False)}"
        )
        response = self._vision.bind(
            response_format={"type": "json_object"},
            extra_body={"enable_thinking": False},
        ).invoke([SystemMessage(content=_VISION_SYSTEM), image_message(prompt, png)])
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
        return value
