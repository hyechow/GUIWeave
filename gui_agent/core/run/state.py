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
) -> ProgramOutcome:
    """Project the external result payload into one typed Program terminal."""
    if isinstance(result, AgentResult):
        return result.to_program_outcome()
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
        output=result.get("output"),
    )


def sync_context_program_outcome(
    context: PolicyContext,
    result: AgentResult | Mapping[str, Any],
) -> None:
    context.outcome = program_outcome_from_result(result)


def write_final_reply(context_path: Path, reply: str) -> None:
    """Persist the frontend reply without mutating the program outcome."""
    raw = json.loads(context_path.read_text(encoding="utf-8"))
    raw["reply"] = reply
    write_json_atomic(context_path, raw)
