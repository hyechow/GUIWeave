"""LLM coding agent for orchestration programs."""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

from langchain_openai import ChatOpenAI

from gui_agent.context import ContextBlock
from gui_agent.context.runtime import file_reference_block, knowledge_block, task_goal_block
from gui_agent.core.config import resolve_llm_config
from gui_agent.core.llm.messages import assemble_messages
from gui_agent.prompts import load_prompt_text

from .models import (
    CodeDiagnostic,
    CodingAttempt,
    CodingEvent,
    CodingPlan,
    CodingReview,
    CodingRunResult,
)
from .sandbox import (
    FixtureSpec,
    build_probe_fixture,
    execute_code,
    validate_code,
    validate_fixture_contract,
    validate_runtime_dataflow,
)


_SYSTEM = load_prompt_text("task.orchestrator.coding")
_REVIEW_SYSTEM = load_prompt_text("task.orchestrator.coding_review")
_CODE_BLOCK_RE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_GENERATION_MAX_OUTPUT_TOKENS = 2048
_REVIEW_MAX_OUTPUT_TOKENS = 4096


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
        dump = {key: value for key, value in dump.items() if key != "match_mode"}
        lines.append(f"- {dump}")
    return ContextBlock(
        id="runtime.intent_facts",
        budget="required",
        source_type="runtime_state",
        source="router",
        ttl="turn",
        priority=20,
        content=(
            "## Router intent facts\n"
            + "\n".join(lines)
            + "\nThese facts describe a mention and its matching semantics, not a required "
            "standalone collection. Query the authoritative source field that also owns the "
            "requested output. Phrase broadening is an orchestration branch: query the full "
            "mention first, then explicitly query the shorter search key only when empty. "
            "Both are strict literal queries."
        ),
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
            "to query from that source only while the program remains in this current context. "
            "After ctx.gui changes application context, do not reuse these collection fields; "
            "use the selected application knowledge for the destination source. Runtime code must "
            "still query and compute from the actual rows."
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


def _diagnostics(
    source: str,
    fixture: FixtureSpec | None,
    *,
    match_lookup_sources: bool = False,
) -> list[Any]:
    diagnostics = validate_code(source)
    diagnostics.extend(validate_runtime_dataflow(source))
    if fixture is not None:
        diagnostics.extend(validate_fixture_contract(
            source,
            fixture,
            match_lookup_sources=match_lookup_sources,
        ))
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
        return "No external fixture was supplied; execution uses schema-only synthetic probe data."

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
        "collection sources (`query` may request only their available_fields):\n"
        f"{lookup_text or '- none'}\n"
        "detail sources (`read` may request only their available_fields):\n"
        f"{detail_text or '- none'}\n"
        f"detail-only fields: {detail_only_fields!r}\n"
        "A detail-only selection field must be read from each concrete collection record before "
        "the Python predicate uses it; never add it to query.\n"
        f"command results: {fixture.command_results!r}"
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
            "The available_fields are the exact mock API schema for this review."
        ),
    )


def _decode_review_response(
    text: str,
) -> tuple[bool, tuple[CodeDiagnostic, ...], str]:
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return False, (), "reviewer returned invalid JSON"
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("approve"), bool)
    ):
        return False, (), "reviewer returned an invalid review object"
    raw_issues = payload.get("issues")
    if not isinstance(raw_issues, list):
        return False, (), "reviewer issues must be a list"
    issues: list[CodeDiagnostic] = []
    for item in raw_issues:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("code"), str)
            or not item["code"].strip()
            or not isinstance(item.get("message"), str)
            or not item["message"].strip()
        ):
            return False, (), "reviewer issue must contain code and message"
        issues.append(CodeDiagnostic(
            code=item["code"].strip(),
            message=item["message"].strip(),
        ))
    approved = payload["approve"]
    if approved == bool(issues):
        return False, (), "reviewer approval and issues conflict"
    return approved, tuple(issues), ""


def _evaluate_source(
    source: str,
    fixture: FixtureSpec | None,
    contract_fixture: FixtureSpec | None = None,
) -> CodingAttempt:
    diagnostics = _diagnostics(
        source,
        fixture or contract_fixture,
        match_lookup_sources=fixture is None and contract_fixture is not None,
    )
    run = execute_code(source, fixture or build_probe_fixture(source)) if not diagnostics else None
    if fixture is None and run is not None and not run.ok:
        final_error = run.error.strip().splitlines()[-1] if run.error.strip() else ""
        definite = (
            "AssertionError", "AttributeError", "TypeError", "NameError",
            "UnboundLocalError", "KeyError", "IndexError", "ValueError",
            "ZeroDivisionError",
        )
        if not final_error.startswith(tuple(f"{name}:" for name in definite)):
            run = CodingRunResult(
                ok=True,
                trace=run.trace,
                writes=run.writes,
                final_state=run.final_state,
            )
    return CodingAttempt(source=source, diagnostics=diagnostics, run=run)


def _attempt_executable(attempt: CodingAttempt) -> bool:
    return not attempt.diagnostics and (attempt.run is None or attempt.run.ok)


def _regeneration_block(
    source: str,
    attempt: CodingAttempt,
) -> ContextBlock:
    issues = [
        *(item.render() for item in attempt.diagnostics),
        *(
            [attempt.run.error]
            if attempt.run is not None and not attempt.run.ok and attempt.run.error
            else []
        ),
    ]
    if not issues:
        issues.append("The candidate failed deterministic validation.")
    return ContextBlock(
        id="runtime.coding_regeneration",
        budget="required",
        source_type="runtime_state",
        source="coding_review",
        ttl="turn",
        priority=1,
        content=(
            "## Rejected candidate\n"
            f"```python\n{source}\n```\n\n"
            "## Issues to resolve\n"
            + "\n".join(f"- {issue}" for issue in issues)
            + "\n\nGenerate a complete replacement program. Preserve correct behavior, but do "
            "not return a patch, edit list, or explanation."
        ),
    )


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
    input_tokens = 0
    output_tokens = 0
    last_error = ""
    last_text = ""
    for attempt_index in range(2):
        try:
            response = review_runner.invoke(messages)
        except Exception as exc:  # noqa: BLE001 - provider/protocol boundary
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt_index == 0:
                continue
            return CodingReview(
                text="",
                approved=False,
                error=f"[REVIEW_IO] {last_error}",
                seconds=time.perf_counter() - started,
            )
        used_in, used_out = _usage(response)
        input_tokens += used_in
        output_tokens += used_out
        last_text = _response_text(response.content)
        approved, issues, error = _decode_review_response(last_text)
        if not error:
            return CodingReview(
                text=last_text,
                approved=approved,
                issues=issues,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                seconds=time.perf_counter() - started,
            )
        last_error = error
        if attempt_index == 0:
            continue
    return CodingReview(
        text=last_text,
        approved=False,
        error=f"[REVIEW_PROTOCOL] {last_error}",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        seconds=time.perf_counter() - started,
    )


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
    on_event: Callable[[CodingEvent], None] | None = None,
) -> CodingPlan:
    """Generate, review, and deterministically regenerate at most once.

    Reviewer output is retained as structured audit evidence. Only static/probe
    failures can trigger the bounded rewrite or block execution.
    """
    events: list[CodingEvent] = []

    def emit(kind: str, **data: Any) -> None:
        event = CodingEvent(kind=kind, data=data)
        events.append(event)
        if on_event is not None:
            on_event(event)

    def emit_validation(phase: str, attempt: CodingAttempt) -> None:
        emit(
            "diagnostics",
            phase=phase,
            status="failed" if attempt.diagnostics else "passed",
            diagnostics=[item.render() for item in attempt.diagnostics],
        )
        run = attempt.run
        emit(
            "probe",
            phase=phase,
            status=(
                "skipped" if run is None
                else "passed" if run.ok
                else "failed"
            ),
            operations=[event.op for event in run.trace] if run is not None else [],
            return_value=repr(run.return_value) if run is not None else "",
            error=run.error if run is not None else "",
        )

    if llm is None:
        llm = _default_llm()
    generator = (
        llm.bind(max_tokens=_GENERATION_MAX_OUTPUT_TOKENS, temperature=0)
        if callable(getattr(llm, "bind", None))
        else llm
    )
    common_blocks: list[ContextBlock | None] = [
        task_goal_block(goal),
        _resolution_block(resolution),
        file_reference_block(file_section),
    ]
    observation_schema = _observation_schema_block(current_observation)
    contract_fixture = _observation_contract_fixture(current_observation)
    review_blocks = [
        *common_blocks,
        knowledge_block("app_knowledge", knowledge),
        _location_block(current_site, current_title, current_url),
        observation_schema,
    ]
    blocks = [
        *review_blocks,
        _fixture_schema_block(fixture),
    ]

    def generate(
        *,
        phase: str,
        extra_blocks: list[ContextBlock | None] | None = None,
    ) -> CodingAttempt:
        started = time.perf_counter()
        emit("generation_started", goal=goal, phase=phase)
        response = generator.invoke(assemble_messages(
            _SYSTEM,
            None,
            human_blocks=[*blocks, *(extra_blocks or [])],
            image_resize="none",
            label="orchestrator.coding_reviewed.generate",
            decision_text="",
        ))
        generated = _extract_source(response.content)
        input_tokens, output_tokens = _usage(response)
        attempt = _evaluate_source(generated, fixture, contract_fixture)
        attempt.input_tokens = input_tokens
        attempt.output_tokens = output_tokens
        attempt.seconds = time.perf_counter() - started
        emit(
            "generation_completed",
            phase=phase,
            source=generated,
            seconds=attempt.seconds,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        emit_validation(phase, attempt)
        return attempt

    def review_attempt(
        attempt: CodingAttempt,
        *,
        pass_index: int,
    ) -> CodingReview:
        emit("review_started", pass_index=pass_index)
        result = _review_attempt(
            llm=llm,
            blocks=review_blocks,
            source=attempt.source,
            attempt=attempt,
            fixture=fixture,
        )
        emit(
            "review_completed",
            pass_index=pass_index,
            approved=result.approved,
            source=attempt.source,
            text=result.text,
            error=result.error,
            issues=[item.render() for item in result.issues],
            seconds=result.seconds,
        )
        return result

    initial = generate(phase="initial")
    attempts = [initial]
    review = review_attempt(initial, pass_index=1)
    current = initial
    regeneration_status = "not_needed"
    if not _attempt_executable(initial):
        current = generate(
            phase="regenerated",
            extra_blocks=[_regeneration_block(initial.source, initial)],
        )
        attempts.append(current)
        regeneration_status = "completed"
        review = review_attempt(current, pass_index=2)

    plan = CodingPlan(
        goal=goal,
        source=current.source,
        attempts=attempts,
        review=review,
        events=events,
    )
    emit(
        "finalized",
        status="passed" if plan.requirements_satisfied else "failed",
        source=current.source,
        repair_status=regeneration_status,
    )
    return plan
