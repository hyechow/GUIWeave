"""Pure ProgramOutcome projection and persistence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from gui_agent.core.run.context import write_json_atomic
from gui_agent.core.run.result import AgentResult
from gui_agent.core.schemas import PolicyContext, ProgramOutcome


def program_outcome_from_result(
    result: AgentResult | Mapping[str, Any],
    output: str | None = None,
) -> ProgramOutcome:
    """Project the external result payload into one typed Program terminal."""
    if isinstance(result, AgentResult):
        return result.to_program_outcome(output=output)
    unknown = set(result) - set(AgentResult.model_fields)
    if unknown:
        raise ValueError(
            "result mapping contains fields outside AgentResult: "
            + ", ".join(sorted(map(str, unknown)))
        )
    return ProgramOutcome(
        phase=result["phase"],
        verification=result.get("verification"),
        summary=str(result.get("summary") or ""),
        output=result.get("output") if output is None else output,
    )


def sync_context_program_outcome(
    context: PolicyContext,
    result: AgentResult | Mapping[str, Any],
    output: str | None = None,
) -> None:
    context.outcome = program_outcome_from_result(result, output=output)


def write_final_program_outcome(
    context_path: Path,
    result: AgentResult | Mapping[str, Any],
    output: str,
) -> None:
    """Patch the immutable ProgramOutcome without rewriting journal events."""
    raw = json.loads(context_path.read_text(encoding="utf-8"))
    raw["outcome"] = program_outcome_from_result(result, output).model_dump(mode="json")
    write_json_atomic(context_path, raw)
