"""LLM coding agent for standalone orchestration programs."""

from __future__ import annotations

import json
import re
import time
from itertools import combinations
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
_LOCAL_REPAIR_RE = re.compile(
    r"<<<<<<< SEARCH\n(.*?)\n=======\n(.*?)\n>>>>>>> REPLACE",
    re.DOTALL,
)
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


def _repair_block(source: str, attempt: CodingAttempt) -> ContextBlock:
    failures = [diagnostic.render() for diagnostic in attempt.diagnostics]
    if attempt.run is not None and not attempt.run.ok:
        failures.append(attempt.run.error)
    trace = attempt.run.trace if attempt.run is not None else []
    return ContextBlock(
        id="runtime.coding_repair",
        budget="required",
        source_type="runtime_state",
        source="coding_sandbox",
        ttl="turn",
        priority=5,
        content=(
            "## Previous source and execution diagnostics\n"
            + "\n".join(f"- {failure}" for failure in failures)
            + f"\ntrace={trace!r}\n\n```python\n{source}\n```\n"
            "Rewrite the complete def run(ctx) and fix the causal business/data-flow error, not "
            "only the failing line. A null projected field means it was unavailable from that "
            "collection: acquire the available selection fields and use ctx.read on each concrete "
            "record when detail state is required. Pass runtime records to interact through named "
            "inputs and literal transition values through required_values. Never "
            "turn a failed required precondition into continue, empty output, or a silent no-op. "
            "ctx.acquire(scope=None, ...) is invalid; establish the intended business set with "
            "ctx.lookup and pass that Scope explicitly. "
            "When a concrete record already exists, read its detail-only fields with "
            "ctx.read(record, fields=[...]); do not lookup/acquire the record ID as a new set. "
            "Do not return a patch."
        ),
    )


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
    collection_fields = {
        str(field)
        for rows in fixture.lookups.values()
        for row in rows
        for field in row
    }
    detail_fields = {
        str(field)
        for state in fixture.reads.values()
        for field in state
    }
    detail_only_fields = sorted(detail_fields - collection_fields)
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


def _review_approved(text: str) -> bool:
    return bool(re.search(r"(?:^|\n)\s*APPROVE\s*$", text, re.IGNORECASE))


def _normalize_review_response(text: str) -> str:
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return text
    if not isinstance(payload, dict):
        return text
    if payload.get("approve") is True:
        return "APPROVE"
    edits = payload.get("edits")
    if not isinstance(edits, list):
        return text
    blocks: list[str] = []
    for edit in edits:
        if not isinstance(edit, dict):
            continue
        search = edit.get("search")
        replacement = edit.get("replacement")
        if not isinstance(search, str) or not isinstance(replacement, str):
            continue
        blocks.append(
            f"<<<<<<< SEARCH\n{search}\n=======\n{replacement}\n>>>>>>> REPLACE"
        )
    return "\n\n".join(blocks) if blocks else text


def _minimize_function_repair(search: str, replacement: str) -> tuple[str, str]:
    """Trim unchanged outer lines from an accidentally broad function edit."""
    search_lines = search.splitlines(keepends=True)
    replacement_lines = replacement.splitlines(keepends=True)
    prefix = 0
    while (
        prefix < len(search_lines)
        and prefix < len(replacement_lines)
        and search_lines[prefix] == replacement_lines[prefix]
    ):
        prefix += 1
    suffix = 0
    while (
        suffix < len(search_lines) - prefix
        and suffix < len(replacement_lines) - prefix
        and search_lines[-suffix - 1] == replacement_lines[-suffix - 1]
    ):
        suffix += 1
    if prefix <= 1 and suffix == 0:
        return search, replacement
    search_end = len(search_lines) - suffix if suffix else len(search_lines)
    replacement_end = (
        len(replacement_lines) - suffix if suffix else len(replacement_lines)
    )
    minimized_search = "".join(search_lines[prefix:search_end])
    minimized_replacement = "".join(replacement_lines[prefix:replacement_end])
    if not minimized_search.strip() and suffix:
        minimized_search = search_lines[search_end]
        minimized_replacement += replacement_lines[replacement_end]
    elif not minimized_search.strip() and prefix > 1:
        minimized_search = search_lines[prefix - 1]
        minimized_replacement = (
            replacement_lines[prefix - 1] + minimized_replacement
        )
    if not minimized_search.strip() or "def run(" in minimized_search:
        return search, replacement
    return minimized_search, minimized_replacement


def _parse_local_repairs(
    source: str,
    response: str,
) -> tuple[list[tuple[str, str]], str]:
    matches = list(dict.fromkeys(_LOCAL_REPAIR_RE.findall(response)))
    if not matches:
        return [], "reviewer returned neither APPROVE nor SEARCH/REPLACE edits"
    if len(matches) > 10:
        return [], "reviewer returned more than ten local edits"
    valid_matches: list[tuple[str, str]] = []
    rejected: list[str] = []
    for search, replacement in matches:
        if not search.strip():
            rejected.append("SEARCH block is empty")
            continue
        run_header = re.search(r"(?m)^def run\(ctx\):[ \t]*$", search)
        if run_header is not None and search[run_header.end():].strip():
            search, replacement = _minimize_function_repair(search, replacement)
            run_header = re.search(r"(?m)^def run\(ctx\):[ \t]*$", search)
            if run_header is not None and search[run_header.end():].strip():
                rejected.append("SEARCH replaces the complete run function")
                continue
        occurrences = source.count(search)
        if occurrences != 1:
            rejected.append(f"SEARCH matched {occurrences} times")
            continue
        valid_matches.append((search, replacement))
    if not valid_matches:
        detail = "; ".join(rejected) or "no valid local edits"
        return [], f"reviewer returned no applicable local repair: {detail}"
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


def _evaluate_source(source: str, fixture: FixtureSpec | None) -> CodingAttempt:
    diagnostics = _diagnostics(source, fixture)
    run = execute_code(source, fixture) if fixture is not None and not diagnostics else None
    return CodingAttempt(source=source, diagnostics=diagnostics, run=run)


def _attempt_executable(attempt: CodingAttempt) -> bool:
    return not attempt.diagnostics and (attempt.run is None or attempt.run.ok)


def _select_local_repair(
    source: str,
    response: str,
    fixture: FixtureSpec | None,
) -> tuple[CodingAttempt, str, tuple[int, ...]]:
    repairs, parse_error = _parse_local_repairs(source, response)
    if parse_error:
        return CodingAttempt(source=source), parse_error, ()

    best_attempt: CodingAttempt | None = None
    best_indices: tuple[int, ...] = ()
    best_score: tuple[int, int, int] | None = None
    repair_counts = (
        range(len(repairs), 0, -1)
        if len(repairs) <= 5
        else (len(repairs),)
    )
    for repair_count in repair_counts:
        for indices in combinations(range(len(repairs)), repair_count):
            candidate, apply_error = _apply_local_repairs(
                source,
                [repairs[index] for index in indices],
            )
            if apply_error:
                continue
            attempt = _evaluate_source(candidate, fixture)
            if _attempt_executable(attempt):
                return attempt, "", indices
            score = (
                0 if not attempt.diagnostics else 1,
                len(attempt.diagnostics),
                -repair_count,
            )
            if best_score is None or score < best_score:
                best_attempt = attempt
                best_indices = indices
                best_score = score

    if best_attempt is None:
        return (
            CodingAttempt(source=source),
            "no non-overlapping local repair candidate could be applied",
            (),
        )
    return best_attempt, "", best_indices


def _review_attempt(
    *,
    reviewer_llm: Any,
    blocks: list[ContextBlock | None],
    source: str,
    attempt: CodingAttempt,
    fixture: FixtureSpec | None,
    context_reports: list[dict[str, Any]] | None,
) -> CodingReview:
    started = time.perf_counter()
    messages = assemble_messages(
        _REVIEW_SYSTEM,
        None,
        human_blocks=[*blocks, _review_evidence_block(source, attempt, fixture)],
        image_resize="none",
        label="orchestrator.coding_reviewed.review",
        context_reports=context_reports,
        decision_text="",
    )
    review_runner = (
        reviewer_llm.bind(
            max_tokens=_REVIEW_MAX_OUTPUT_TOKENS,
            temperature=0,
            response_format={"type": "json_object"},
        )
        if callable(getattr(reviewer_llm, "bind", None))
        else reviewer_llm
    )
    response = review_runner.invoke(messages)
    raw_text = _response_text(response.content)
    text = _normalize_review_response(raw_text)
    input_tokens, output_tokens = _usage(response)
    review = CodingReview(
        text=text,
        approved=_review_approved(text),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        seconds=time.perf_counter() - started,
    )
    if context_reports is not None:
        context_reports.append({
            "kind": "coding_review",
            "source": source,
            "review": text,
            "raw_review": raw_text,
            "approved": review.approved,
            "usage_metadata": getattr(response, "usage_metadata", None),
        })
    return review


def generate_code(
    goal: str,
    *,
    knowledge: str = "",
    resolution: Any = None,
    file_section: str = "",
    current_site: str = "",
    current_title: str = "",
    current_url: str = "",
    fixture: FixtureSpec | None = None,
    llm: Any = None,
    context_reports: list[dict[str, Any]] | None = None,
) -> CodingPlan:
    """Generate, statically check and optionally fixture-run a coding plan.

    One repair is allowed for syntax/safety failures, missing business assertions,
    execution failures, or failed assertions. External graders remain private.
    """
    if llm is None:
        llm = _default_llm()

    blocks: list[ContextBlock | None] = [
        task_goal_block(goal),
        _resolution_block(resolution),
        file_reference_block(file_section),
        knowledge_block("app_knowledge", knowledge),
        _location_block(current_site, current_title, current_url),
    ]
    attempts: list[CodingAttempt] = []
    repair: ContextBlock | None = None
    source = ""
    for attempt_index in range(2):
        started = time.perf_counter()
        messages = assemble_messages(
            _SYSTEM,
            None,
            human_blocks=[*blocks, repair],
            image_resize="none",
            label="orchestrator.coding",
            context_reports=context_reports,
            decision_text="",
        )
        response = llm.invoke(messages)
        source = _extract_source(response.content)
        input_tokens, output_tokens = _usage(response)
        diagnostics = _diagnostics(source, fixture)
        run = execute_code(source, fixture) if fixture is not None and not diagnostics else None
        attempt = CodingAttempt(
            source=source,
            diagnostics=diagnostics,
            run=run,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            seconds=time.perf_counter() - started,
        )
        attempts.append(attempt)
        if context_reports is not None:
            context_reports.append({
                "kind": "coding_source",
                "attempt": attempt_index,
                "source": source,
                "diagnostics": [diagnostic.render() for diagnostic in diagnostics],
                "run_error": run.error if run is not None else "",
                "usage_metadata": getattr(response, "usage_metadata", None),
            })
        if not diagnostics and (run is None or run.ok):
            break
        repair = _repair_block(source, attempt)
    return CodingPlan(goal=goal, source=source, attempts=attempts)


def generate_reviewed_code(
    goal: str,
    *,
    knowledge: str = "",
    resolution: Any = None,
    file_section: str = "",
    current_site: str = "",
    current_title: str = "",
    current_url: str = "",
    fixture: FixtureSpec | None = None,
    llm: Any = None,
    reviewer_llm: Any = None,
    context_reports: list[dict[str, Any]] | None = None,
) -> CodingPlan:
    """Generate once, then apply one locally constrained review repair."""
    if llm is None:
        llm = _default_llm()
    if reviewer_llm is None:
        reviewer_llm = llm
    blocks: list[ContextBlock | None] = [
        task_goal_block(goal),
        _resolution_block(resolution),
        file_reference_block(file_section),
        knowledge_block("app_knowledge", knowledge),
        _location_block(current_site, current_title, current_url),
        _fixture_schema_block(fixture),
    ]
    review_blocks: list[ContextBlock | None] = [
        task_goal_block(goal),
        _resolution_block(resolution),
        file_reference_block(file_section),
    ]

    started = time.perf_counter()
    response = llm.invoke(assemble_messages(
        _SYSTEM,
        None,
        human_blocks=blocks,
        image_resize="none",
        label="orchestrator.coding_reviewed.generate",
        context_reports=context_reports,
        decision_text="",
    ))
    source = _extract_source(response.content)
    input_tokens, output_tokens = _usage(response)
    diagnostics = _diagnostics(source, fixture)
    run = execute_code(source, fixture) if fixture is not None and not diagnostics else None
    initial = CodingAttempt(
        source=source,
        diagnostics=diagnostics,
        run=run,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        seconds=time.perf_counter() - started,
    )
    attempts = [initial]
    review = _review_attempt(
        reviewer_llm=reviewer_llm,
        blocks=review_blocks,
        source=source,
        attempt=initial,
        fixture=fixture,
        context_reports=context_reports,
    )
    if not review.approved:
        repaired, repair_error, selected_repairs = _select_local_repair(
            source,
            review.text,
            fixture,
        )
        if repair_error:
            repaired.diagnostics = [
                CodeDiagnostic("LOCAL_REPAIR_INVALID", repair_error),
            ]
        source = repaired.source
        attempts.append(repaired)
        if context_reports is not None:
            context_reports.append({
                "kind": "coding_local_repair",
                "selected_repairs": list(selected_repairs),
                "diagnostics": [
                    diagnostic.render()
                    for diagnostic in repaired.diagnostics
                ],
                "run_error": repaired.run.error if repaired.run is not None else "",
            })
    return CodingPlan(
        goal=goal,
        source=source,
        attempts=attempts,
        review=review,
        reviews=[review],
    )


__all__ = ["generate_code", "generate_reviewed_code"]
