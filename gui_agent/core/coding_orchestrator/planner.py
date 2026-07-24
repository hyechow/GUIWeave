"""LLM coding agent for standalone orchestration programs."""

from __future__ import annotations

import json
import re
import time
from typing import Any

from langchain_openai import ChatOpenAI

from gui_agent.context import ContextBlock
from gui_agent.context.runtime import file_reference_block, knowledge_block, task_goal_block
from gui_agent.core.config import resolve_llm_config
from gui_agent.core.llm.messages import assemble_messages
from gui_agent.prompts import load_prompt_text

from .models import CodeDiagnostic, CodingAttempt, CodingPlan, CodingReview
from .sandbox import (
    FixtureSpec,
    execute_code,
    validate_code,
    validate_fixture_contract,
    validate_projection_contract,
    validate_runtime_dataflow,
)


_SYSTEM = load_prompt_text("task.orchestrator.coding")
_REVIEW_SYSTEM = load_prompt_text("task.orchestrator.coding_review")
_CODE_BLOCK_RE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_REVIEW_MAX_OUTPUT_TOKENS = 3072


def _response_text(content: Any) -> str:
    if isinstance(content, list):
        content = "\n".join(
            str(item.get("text") or "") if isinstance(item, dict) else str(item)
            for item in content
        )
    return str(content or "").strip()


def _extract_source(content: Any) -> str:
    text = _response_text(content)
    match = _CODE_BLOCK_RE.search(text)
    return (match.group(1) if match else text).strip()


def _resolution_block(resolution: Any) -> ContextBlock | None:
    entities = getattr(resolution, "entities", None)
    if not entities:
        return None
    lines = []
    for entity in entities:
        dump = entity.model_dump() if hasattr(entity, "model_dump") else vars(entity)
        lines.append(f"- {dump}")
    return ContextBlock(
        id="runtime.intent_facts",
        budget="required",
        source_type="runtime_state",
        source="router",
        ttl="turn",
        priority=20,
        content="## Router intent facts\n" + "\n".join(lines),
    )


def _location_block(site: str, title: str, url: str) -> ContextBlock | None:
    if not any((site, title, url)):
        return None
    return ContextBlock(
        id="runtime.location",
        budget="required",
        source_type="runtime_state",
        source="observation",
        ttl="turn",
        priority=30,
        content=f"## Current location\nsite={site!r}\ntitle={title!r}\nurl={url!r}",
    )


def _observation_schema_block(observation: Any) -> ContextBlock | None:
    if observation is None:
        return None
    tables = getattr(observation, "tables", None) or []
    table_schema = []
    for table in tables[:20]:
        if not isinstance(table, dict):
            continue
        caption = str(table.get("caption") or "").strip()
        headers = [
            str(header)
            for header in (table.get("headers") or [])
            if str(header).strip()
        ]
        if caption or headers:
            table_schema.append({"source": caption, "fields": headers})

    controls = getattr(observation, "form_controls", None) or []
    control_schema = []
    for control in controls[:40]:
        if not isinstance(control, dict):
            continue
        label = str(
            control.get("label")
            or control.get("name")
            or control.get("id")
            or ""
        ).strip()
        kind = str(control.get("kind") or control.get("type") or "").strip()
        if label:
            control_schema.append({"field": label, "kind": kind})

    if not table_schema and not control_schema:
        return None
    schema = {
        "collections": table_schema,
        "controls": control_schema,
    }
    return ContextBlock(
        id="runtime.current_view_schema",
        budget="required",
        source_type="runtime_state",
        source="observation",
        ttl="turn",
        priority=14,
        content=(
            "## Current-view interface schema\n"
            f"{json.dumps(schema, ensure_ascii=False)}\n"
            "This is an interface schema, not task-result data. A collection source names the "
            "business scope to establish, and its fields are the exact semantic names available "
            "to acquire from that source. Runtime code must still acquire and compute from the "
            "actual rows."
        ),
    )


def _observation_contract_fixture(observation: Any) -> FixtureSpec | None:
    if observation is None:
        return None
    lookups: dict[str, list[dict[str, Any]]] = {}
    for table in getattr(observation, "tables", None) or []:
        if not isinstance(table, dict):
            continue
        caption = str(table.get("caption") or "").strip()
        fields = [
            str(header)
            for header in table.get("headers") or []
            if str(header).strip()
        ]
        if caption and fields:
            lookups[caption.casefold()] = [{field: None for field in fields}]
    return FixtureSpec(lookups=lookups) if lookups else None


def _usage(response: Any) -> tuple[int, int]:
    usage = getattr(response, "usage_metadata", None) or {}
    return int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)


def _diagnostics(source: str, fixture: FixtureSpec | None) -> list[Any]:
    diagnostics = validate_code(source)
    diagnostics.extend(validate_projection_contract(source))
    diagnostics.extend(validate_runtime_dataflow(source))
    if fixture is not None:
        diagnostics.extend(validate_fixture_contract(source, fixture))
    return diagnostics


def _default_llm() -> Any:
    cfg = resolve_llm_config("supervisor.decompose")
    if not cfg.model:
        cfg = resolve_llm_config("supervisor")
    from llm.provider_config import dashscope_extra_body

    return ChatOpenAI(
        model=cfg.model,
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        extra_body=dashscope_extra_body(cfg.model),
    )


def _fixture_review_text(fixture: FixtureSpec | None) -> str:
    if fixture is None:
        return "No mock fixture was supplied."

    def examples(rows: list[dict[str, Any]]) -> dict[str, list[Any]]:
        result: dict[str, list[Any]] = {}
        identity_fields = {
            "id", "orderid", "sku", "name", "customername", "actionurl", "url",
        }
        for row in rows:
            for field, value in row.items():
                if (
                    re.sub(r"[\W_]+", "", str(field)).casefold() in identity_fields
                    or isinstance(value, (dict, list))
                ):
                    continue
                values = result.setdefault(str(field), [])
                if value not in values and len(values) < 3:
                    values.append(value)
        return result

    groups: list[tuple[list[str], list[dict[str, Any]]]] = []
    for alias, rows in fixture.lookups.items():
        existing = next((group for group in groups if group[1] == rows), None)
        if existing is None:
            groups.append(([alias], rows))
        else:
            existing[0].append(alias)
    lookup_text = "\n".join(
        (
            f"- aliases={aliases!r}\n"
            f"  available_fields={sorted({str(field) for row in rows for field in row})!r}\n"
            f"  example_values={examples(rows)!r}"
        )
        for aliases, rows in groups
    )
    detail_groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for state in fixture.reads.values():
        fields = tuple(sorted(str(field) for field in state))
        detail_groups.setdefault(fields, []).append(state)
    detail_text = "\n".join(
        (
            f"- available_fields={list(fields)!r}\n"
            f"  example_values={examples(states)!r}"
        )
        for fields, states in sorted(detail_groups.items())
    )
    collection_fields = fixture.fields()
    detail_fields = fixture.fields(include_reads=True) - collection_fields
    detail_only_fields = sorted(detail_fields)
    return (
        "collection sources (`acquire` may request only their available_fields):\n"
        f"{lookup_text or '- none'}\n"
        "detail sources (`read` may request only their available_fields):\n"
        f"{detail_text or '- none'}\n"
        f"detail-only fields: {detail_only_fields!r}\n"
        "A detail-only selection field must be read from each concrete collection record before "
        "the Python predicate uses it; never add it to acquire.\n"
        f"command results: {fixture.command_results!r}\n"
        f"compute results: {fixture.compute_results!r}"
    )


def _fixture_schema_block(fixture: FixtureSpec | None) -> ContextBlock | None:
    if fixture is None:
        return None
    return ContextBlock(
        id="runtime.coding_api_schema",
        budget="required",
        source_type="runtime_state",
        source="coding_mock_schema",
        ttl="turn",
        priority=15,
        content=(
            "## Mock API field contract\n"
            f"{_fixture_review_text(fixture)}\n"
            "This is an interface schema, not a canonical answer. Use only these exact source "
            "fields, but derive targets and values from the user task and runtime data."
        ),
    )


def _review_evidence_block(
    source: str,
    attempt: CodingAttempt,
    fixture: FixtureSpec | None,
) -> ContextBlock:
    diagnostics = "\n".join(
        f"- {diagnostic.render()}"
        for diagnostic in attempt.diagnostics
    ) or "- none"
    run = attempt.run
    if attempt.diagnostics:
        runtime_status = "NOT_RUN: static diagnostics must be fixed first"
    elif run is None:
        runtime_status = "NOT_RUN: no mock fixture was supplied"
    elif run.ok:
        runtime_status = "PASS"
    else:
        runtime_status = "FAIL"
    return ContextBlock(
        id="runtime.coding_review_evidence",
        budget="required",
        source_type="runtime_state",
        source="coding_mock_runtime",
        ttl="turn",
        priority=5,
        content=(
            "## Candidate solution.py\n"
            f"```python\n{source}\n```\n\n"
            f"## Static validation\n{diagnostics}\n\n"
            f"## Mock runtime status\n{runtime_status}\n\n"
            f"## Runtime error\n{run.error if run is not None else '- not available'}\n\n"
            f"## Observed execution trace\n{run.trace if run is not None else []!r}\n\n"
            f"## Return value\n{run.return_value if run is not None else None!r}\n\n"
            f"## Mocked Statement effects with available before/after state\n"
            f"{run.writes if run is not None else []!r}\n\n"
            "## Mock API schema\n"
            f"{_fixture_review_text(fixture)}\n"
            "The available_fields are the exact mock API schema for this review.\n\n"
            "## Mandatory final repair gate\n"
            f"Static diagnostics that must all disappear:\n{diagnostics}\n"
            f"Runtime status that must become PASS: {runtime_status}\n"
            f"Runtime error that must disappear:\n"
            f"{run.error if run is not None else '- not available'}\n"
            "Before returning edits, mentally apply the complete patch set once. Every newly "
            "accessed acquire/read field must be present in that record's projection, every "
            "listed diagnostic must be fixed, and every requested mutation literal must reach "
            "required_values. Every newly added assert must have a nonempty diagnostic message. "
            "Do not return a partial repair."
        ),
    )


def _decode_review_response(
    text: str,
) -> tuple[bool, tuple[tuple[str, str], ...], str]:
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return False, (), "reviewer returned invalid JSON"
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("approve"), bool)
    ):
        return False, (), "reviewer returned an invalid review object"
    edits = payload.get("edits")
    if not isinstance(edits, list):
        return False, (), "reviewer edits must be a list"
    repairs: list[tuple[str, str]] = []
    for edit in edits:
        if not isinstance(edit, dict):
            return False, (), "reviewer edit must be an object"
        search = edit.get("search")
        replacement = edit.get("replacement")
        if not isinstance(search, str) or not isinstance(replacement, str):
            return False, (), "reviewer edit search and replacement must be strings"
        repairs.append((search, replacement))
    approved = payload["approve"]
    if approved == bool(repairs):
        return False, (), "reviewer approval and edits conflict"
    return approved, tuple(repairs), ""


def _parse_local_repairs(
    source: str,
    repairs: tuple[tuple[str, str], ...],
) -> tuple[list[tuple[str, str]], str]:
    if not repairs:
        return [], "reviewer returned no local edits"
    matches = list(dict.fromkeys(repairs))
    if len(matches) > 10:
        return [], "reviewer returned more than ten local edits"
    valid_matches: list[tuple[str, str]] = []
    rejected: list[str] = []
    for search, replacement in matches:
        if not search.strip():
            rejected.append("SEARCH block is empty")
            continue
        occurrences = source.count(search)
        if occurrences != 1:
            rejected.append(f"SEARCH matched {occurrences} times")
            continue
        valid_matches.append((search, replacement))
    if not valid_matches:
        return [], (
            "reviewer returned no applicable local repair: "
            + ("; ".join(rejected) or "no valid local edits")
        )
    return valid_matches, ""


def _apply_local_repairs(
    source: str,
    repairs: list[tuple[str, str]],
) -> tuple[str, str]:
    repaired = source
    for search, replacement in repairs:
        occurrences = repaired.count(search)
        if occurrences != 1:
            return source, (
                "local edits overlap or depend on another replacement; "
                f"SEARCH matched {occurrences} times after earlier edits"
            )
        repaired = repaired.replace(search, replacement, 1)
    return repaired, ""


def _evaluate_source(
    source: str,
    fixture: FixtureSpec | None,
    contract_fixture: FixtureSpec | None = None,
) -> CodingAttempt:
    diagnostics = _diagnostics(source, fixture or contract_fixture)
    run = execute_code(source, fixture) if fixture is not None and not diagnostics else None
    return CodingAttempt(source=source, diagnostics=diagnostics, run=run)


def _attempt_executable(attempt: CodingAttempt) -> bool:
    return not attempt.diagnostics and (attempt.run is None or attempt.run.ok)


def _select_local_repair(
    source: str,
    repairs: tuple[tuple[str, str], ...],
    fixture: FixtureSpec | None,
    contract_fixture: FixtureSpec | None = None,
) -> tuple[CodingAttempt, str, tuple[int, ...]]:
    repairs, parse_error = _parse_local_repairs(source, repairs)
    if parse_error:
        return CodingAttempt(source=source), parse_error, ()

    candidate, apply_error = _apply_local_repairs(source, repairs)
    if not apply_error:
        attempt = _evaluate_source(candidate, fixture, contract_fixture)
        if _attempt_executable(attempt):
            return attempt, "", tuple(range(len(repairs)))

    current = _evaluate_source(source, fixture, contract_fixture)
    selected: list[int] = []
    for index, repair in enumerate(repairs):
        candidate, apply_error = _apply_local_repairs(current.source, [repair])
        if apply_error:
            continue
        attempt = _evaluate_source(candidate, fixture, contract_fixture)
        # Static/mock gates can reject a bad semantic edit, but cannot prove that
        # a valid semantic edit improves an already-executable program. The
        # Reviewer owns that judgment, so retain every independently executable
        # local edit after discarding invalid ones.
        if _attempt_executable(attempt):
            current = attempt
            selected.append(index)
    return current, "", tuple(selected)


def _review_attempt(
    *,
    llm: Any,
    blocks: list[ContextBlock | None],
    source: str,
    attempt: CodingAttempt,
    fixture: FixtureSpec | None,
) -> CodingReview:
    started = time.perf_counter()
    messages = assemble_messages(
        _REVIEW_SYSTEM,
        None,
        human_blocks=[*blocks, _review_evidence_block(source, attempt, fixture)],
        image_resize="none",
        label="orchestrator.coding_reviewed.review",
        decision_text="",
    )
    review_runner = (
        llm.bind(
            max_tokens=_REVIEW_MAX_OUTPUT_TOKENS,
            temperature=0,
            response_format={"type": "json_object"},
        )
        if callable(getattr(llm, "bind", None))
        else llm
    )
    response = review_runner.invoke(messages)
    raw_text = _response_text(response.content)
    approved, edits, error = _decode_review_response(raw_text)
    input_tokens, output_tokens = _usage(response)
    review = CodingReview(
        text=raw_text,
        approved=approved,
        edits=edits,
        error=error,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        seconds=time.perf_counter() - started,
    )
    return review


def generate_reviewed_code(
    goal: str,
    *,
    knowledge: str = "",
    resolution: Any = None,
    file_section: str = "",
    current_site: str = "",
    current_title: str = "",
    current_url: str = "",
    current_observation: Any = None,
    fixture: FixtureSpec | None = None,
    llm: Any = None,
) -> CodingPlan:
    """Generate once, then apply one locally constrained review repair."""
    if llm is None:
        llm = _default_llm()
    common_blocks: list[ContextBlock | None] = [
        task_goal_block(goal),
        _resolution_block(resolution),
        file_reference_block(file_section),
    ]
    observation_schema = _observation_schema_block(current_observation)
    contract_fixture = _observation_contract_fixture(current_observation)
    blocks = [
        *common_blocks,
        knowledge_block("app_knowledge", knowledge),
        _location_block(current_site, current_title, current_url),
        observation_schema,
        _fixture_schema_block(fixture),
    ]

    started = time.perf_counter()
    response = llm.invoke(assemble_messages(
        _SYSTEM,
        None,
        human_blocks=blocks,
        image_resize="none",
        label="orchestrator.coding_reviewed.generate",
        decision_text="",
    ))
    source = _extract_source(response.content)
    input_tokens, output_tokens = _usage(response)
    initial = _evaluate_source(source, fixture, contract_fixture)
    initial.input_tokens = input_tokens
    initial.output_tokens = output_tokens
    initial.seconds = time.perf_counter() - started
    attempts = [initial]
    review = _review_attempt(
        llm=llm,
        blocks=[*common_blocks, observation_schema],
        source=source,
        attempt=initial,
        fixture=fixture,
    )
    if not review.approved:
        if review.error:
            repaired = CodingAttempt(source=source)
            repair_error, selected_repairs = review.error, ()
        else:
            repaired, repair_error, selected_repairs = _select_local_repair(
                source,
                review.edits,
                fixture,
                contract_fixture,
            )
        if repair_error:
            repaired.diagnostics = [
                CodeDiagnostic("LOCAL_REPAIR_INVALID", repair_error),
            ]
        repair_accepted = bool(selected_repairs) and _attempt_executable(repaired)
        if repair_error or repair_accepted or not _attempt_executable(initial):
            source = repaired.source
            attempts.append(repaired)
    return CodingPlan(
        goal=goal,
        source=source,
        attempts=attempts,
        review=review,
    )
