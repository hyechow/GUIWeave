"""LLM coding agent for standalone orchestration programs."""

from __future__ import annotations

import re
import time
from typing import Any

from langchain_openai import ChatOpenAI

from gui_agent.context import ContextBlock
from gui_agent.context.runtime import file_reference_block, knowledge_block, task_goal_block
from gui_agent.core.config import resolve_llm_config
from gui_agent.core.llm.messages import assemble_messages
from gui_agent.prompts import load_prompt_text

from .models import CodingAttempt, CodingPlan
from .sandbox import FixtureSpec, execute_code, validate_code


_SYSTEM = load_prompt_text("task.orchestrator.coding")
_CODE_BLOCK_RE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def _extract_source(content: Any) -> str:
    if isinstance(content, list):
        content = "\n".join(
            str(item.get("text") or "") if isinstance(item, dict) else str(item)
            for item in content
        )
    text = str(content or "").strip()
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
            "collection: acquire stable identity and use ctx.read on each concrete target. Never "
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
        cfg = resolve_llm_config("supervisor.decompose")
        if not cfg.model:
            cfg = resolve_llm_config("supervisor")
        from llm.provider_config import dashscope_extra_body
        llm = ChatOpenAI(
            model=cfg.model,
            api_key=cfg.api_key,
            base_url=cfg.base_url,
            extra_body=dashscope_extra_body(cfg.model),
        )

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
        diagnostics = validate_code(source)
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


__all__ = ["generate_code"]
