"""Pure ProgramOutcome projection and persistence."""

from __future__ import annotations

import json
from pathlib import Path

from gui_agent.core.schemas import PolicyContext, ProgramOutcome


def program_outcome_from_result(result: dict, output: str | None = None) -> ProgramOutcome:
    """Project the external result payload into one typed Program terminal."""
    return ProgramOutcome(
        phase=result["phase"],
        verification=result.get("verification"),
        summary=str(result.get("stop_reason") or ""),
        output=output,
    )


def sync_context_program_outcome(
    context: PolicyContext,
    result: dict,
    output: str | None = None,
) -> None:
    context.outcome = program_outcome_from_result(result, output=output)


def write_final_program_outcome(context_path: Path, result: dict, output: str) -> None:
    """Patch the immutable ProgramOutcome without rewriting journal events."""
    raw = json.loads(context_path.read_text(encoding="utf-8"))
    raw["outcome"] = program_outcome_from_result(result, output).model_dump(mode="json")
    context_path.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
    )
