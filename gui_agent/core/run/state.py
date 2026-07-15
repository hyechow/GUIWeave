"""Pure ProgramOutcome projection and persistence."""

from __future__ import annotations

import json
from pathlib import Path

from gui_agent.core.schemas import PolicyContext, ProgramOutcome


def program_outcome_from_result(result: dict, output: str | None = None) -> ProgramOutcome:
    """Project the external result payload into one typed Program terminal."""
    stop_reason = str(result.get("stop_reason") or "")
    if result.get("execution_completed"):
        verification = (
            "accepted_unverified"
            if result.get("goal_status") == "accepted_unverified"
            else "confirmed"
        )
        return ProgramOutcome(
            phase="completed",
            verification=verification,
            summary=stop_reason,
            output=output,
        )
    if "ESC" in stop_reason or "用户退出" in stop_reason or "用户按" in stop_reason:
        phase = "interrupted"
    elif bool((result.get("orchestrator") or {}).get("failed")):
        phase = "failed"
    else:
        phase = "stopped"
    return ProgramOutcome(
        phase=phase,
        summary=stop_reason,
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
