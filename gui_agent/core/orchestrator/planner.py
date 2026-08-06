"""LLM coding agent for orchestration programs."""

from __future__ import annotations

import ast
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
    CodingRunResult,
)
from .sandbox import (
    FixtureSpec,
    build_probe_fixture,
    execute_code,
    repair_direct_read_fields,
    validate_code,
    validate_fixture_contract,
    validate_runtime_dataflow,
    validate_commit_reference_dataflow,
)


_SYSTEM = load_prompt_text("task.orchestrator.coding")
_CODE_BLOCK_RE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_GENERATION_MAX_OUTPUT_TOKENS = 2048
_MAX_REGENERATIONS = 3


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


def _semantic_supplement_block(supplement: str) -> ContextBlock | None:
    if not supplement:
        return None
    return ContextBlock(
        id="runtime.task.semantic_supplement",
        budget="required",
        source_type="runtime_state",
        source="router",
        ttl="task",
        priority=21,
        content=(
            "## Semantic supplement\n"
            f"{supplement}\n"
            "This adds only implicit meaning. The original user task remains authoritative for "
            "all explicit names, values, qualifiers, operations, and output requirements."
        ),
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
            "After ctx.reach changes application context, do not reuse these collection fields; "
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
    diagnostics.extend(validate_commit_reference_dataflow(source))
    if fixture is not None:
        diagnostics.extend(validate_fixture_contract(
            source,
            fixture,
            match_lookup_sources=match_lookup_sources,
        ))
    return diagnostics


def _default_llm() -> Any:
    cfg = resolve_llm_config("orchestrator")
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


def _unstructured_visual_block() -> ContextBlock:
    return ContextBlock(
        id="runtime.unstructured_visual_contract",
        budget="required",
        source_type="runtime_state",
        source="coding_api",
        ttl="turn",
        priority=2,
        content=(
            "## Required unstructured visual-source contract\n"
            "No structured source schema is available. For a visible scalar or named value, use "
            "one semantic result/view `ctx.reach` followed by one typed direct `ctx.read`. Name "
            "the result entity, not the current application, and name the same requested fields "
            "in `success.fields` and `read.fields`; return the keyed value. Generic whole-page "
            "fields and display-text parsing are allowed only when the user explicitly requests "
            "that content."
        ),
    )


def _unstructured_visual_diagnostics(source: str) -> list[CodeDiagnostic]:
    """Reject invented collections when the planner has no structured source schema."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    query = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "ctx"
            and node.func.attr == "query"
        ),
        None,
    )
    if query is None:
        return []
    return [CodeDiagnostic(
        "UNSTRUCTURED_QUERY_FORBIDDEN",
        (
            "no structured collection schema was supplied, so ctx.query would invent an entity "
            "or fields. Replace the query with one result/view ctx.reach whose success.fields "
            "declares the requested visible fields, then one typed direct ctx.read"
        ),
        query.lineno,
        query.col_offset + 1,
    )]


def _evaluate_source(
    source: str,
    fixture: FixtureSpec | None,
    contract_fixture: FixtureSpec | None = None,
    unstructured_visual: bool = False,
) -> CodingAttempt:
    """Structural compile only: AST safety, contracts, dataflow, optional visual mode.

    Task meaning (goal text / knowledge markdown) is never matched here. Business
    shape belongs in eval contracts; generation guidance belongs in prompts.
    """
    diagnostics = _diagnostics(
        source,
        fixture or contract_fixture,
        match_lookup_sources=fixture is None and contract_fixture is not None,
    )
    if unstructured_visual:
        diagnostics.extend(_unstructured_visual_diagnostics(source))
    run = execute_code(source, fixture or build_probe_fixture(source)) if not diagnostics else None
    if fixture is None and run is not None and not run.ok:
        final_error = run.error.strip().splitlines()[-1] if run.error.strip() else ""
        definite = (
            "AttributeError", "TypeError", "NameError",
            "UnboundLocalError", "KeyError", "IndexError",
            "ZeroDivisionError",
        )
        business_value_error = (
            final_error.startswith("ValueError:")
            and any(event.op in {"query", "read"} for event in run.trace)
        )
        is_definite = (
            final_error.startswith(tuple(f"{name}:" for name in definite))
            or final_error.startswith("ValueError:") and not business_value_error
        )
        if not is_definite:
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
    known_issues: list[str] | None = None,
) -> ContextBlock:
    current_issues = [
        *(item.render() for item in attempt.diagnostics),
        *(
            [attempt.run.error]
            if attempt.run is not None and not attempt.run.ok and attempt.run.error
            else []
        ),
    ]
    issues = list(dict.fromkeys([*(known_issues or []), *current_issues]))
    if not issues:
        issues.append("The candidate failed deterministic validation.")
    return ContextBlock(
        id="runtime.coding_regeneration",
        budget="required",
        source_type="runtime_state",
        source="coding_compile",
        ttl="turn",
        priority=1,
        content=(
            "## Rejected candidate\n"
            f"```python\n{source}\n```\n\n"
            "## Issues to resolve\n"
            + "\n".join(f"- {issue}" for issue in issues)
            + "\n\nDiscard the rejected implementation choices and re-derive a complete "
            "replacement program from the task and API contracts. Do not carry forward a source, "
            "field, entity, or parsing approach merely because the rejected candidate used it. "
            "Return only the complete program, not a patch, edit list, or explanation."
        ),
    )


def generate_code(
    goal: str,
    *,
    knowledge: str = "",
    platform_contract: str = "",
    resolution: Any = None,
    file_section: str = "",
    current_site: str = "",
    current_title: str = "",
    current_url: str = "",
    current_observation: Any = None,
    fixture: FixtureSpec | None = None,
    llm: Any = None,
    temperature: float = 0.0,
    on_event: Callable[[CodingEvent], None] | None = None,
) -> CodingPlan:
    """Generate and review a program, with bounded whole-program regeneration."""
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
        llm.bind(
            max_tokens=_GENERATION_MAX_OUTPUT_TOKENS,
            temperature=temperature,
        )
        if callable(getattr(llm, "bind", None))
        else llm
    )
    semantic_supplement = str(
        getattr(resolution, "semantic_supplement", "") or ""
    ).strip()
    common_blocks: list[ContextBlock | None] = [
        task_goal_block(goal),
        file_reference_block(file_section),
    ]
    observation_schema = _observation_schema_block(current_observation)
    contract_fixture = _observation_contract_fixture(current_observation)
    unstructured_visual = (
        _unstructured_visual_block()
        if (
            fixture is None
            and observation_schema is None
            and not knowledge.strip()
        )
        else None
    )
    app_knowledge = knowledge_block("app_knowledge", knowledge)
    system_blocks = [
        app_knowledge,
        _semantic_supplement_block(semantic_supplement),
    ]
    blocks = [
        *common_blocks,
        (
            ContextBlock(
                id="runtime.platform_contract",
                budget="required",
                source_type="prompt_asset",
                source="platform_orchestrator",
                ttl="session",
                priority=10,
                content=platform_contract,
            )
            if platform_contract
            else None
        ),
        _location_block(current_site, current_title, current_url),
        observation_schema,
        _fixture_schema_block(fixture),
        unstructured_visual,
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
            system_blocks=system_blocks,
            human_blocks=[*blocks, *(extra_blocks or [])],
            image_resize="none",
            label="orchestrator.coding.generate",
            decision_text="",
        ))
        generated = _extract_source(response.content)
        input_tokens, output_tokens = _usage(response)
        attempt = _evaluate_source(
            generated,
            fixture,
            contract_fixture,
            unstructured_visual is not None,
        )
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

    initial = generate(phase="initial")
    attempts = [initial]
    repair_status = "not_needed"

    def apply_direct_read_repair(
        attempt: CodingAttempt,
    ) -> CodingAttempt | None:
        if (
            _attempt_executable(attempt)
            or not attempt.diagnostics
            or any(
                item.code != "DIRECT_READ_FIELDS_UNDECLARED"
                for item in attempt.diagnostics
            )
        ):
            return None
        repaired_source = repair_direct_read_fields(attempt.source)
        if repaired_source is None:
            return None
        repaired = _evaluate_source(
            repaired_source,
            fixture,
            contract_fixture,
            unstructured_visual is not None,
        )
        attempts.append(repaired)
        emit(
            "deterministic_repair_completed",
            repair="direct_read_fields",
            source=repaired_source,
        )
        emit_validation("deterministic_repair", repaired)
        return repaired

    current = apply_direct_read_repair(initial) or initial
    if current is not initial:
        repair_status = "deterministic"
    known_issues: list[str] = []
    for regeneration in range(1, _MAX_REGENERATIONS + 1):
        if _attempt_executable(current):
            break
        current_issues = [
            *(item.render() for item in current.diagnostics),
            *(
                [current.run.error]
                if current.run is not None and not current.run.ok and current.run.error
                else []
            ),
        ]
        known_issues = list(dict.fromkeys([*known_issues, *current_issues]))
        current = generate(
            phase=(
                "regenerated"
                if regeneration == 1
                else f"regenerated_{regeneration}"
            ),
            extra_blocks=[_regeneration_block(current.source, current, known_issues)],
        )
        attempts.append(current)
        repair_status = "completed"
        repaired = apply_direct_read_repair(current)
        if repaired is not None:
            current = repaired
            repair_status = "deterministic"

    plan = CodingPlan(
        goal=goal,
        source=current.source,
        attempts=attempts,
        events=events,
    )
    emit(
        "finalized",
        status="passed" if plan.requirements_satisfied else "failed",
        source=current.source,
        repair_status=repair_status,
    )
    return plan
