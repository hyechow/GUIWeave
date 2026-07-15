"""Task-level run state persistence (no per-statement snapshot dual-write)."""

from __future__ import annotations

import json
from pathlib import Path

from gui_agent.core.schemas import PolicyContext, RunState


def classify_run_status(result: dict) -> str:
    """Classify a finished run for reports without relying on LLM wording."""
    if result.get("execution_completed") or result.get("goal_completed"):
        return "completed"
    stop_reason = str(result.get("stop_reason") or "")
    if "ESC" in stop_reason or "用户退出" in stop_reason or "用户按" in stop_reason:
        return "interrupted"
    return "stopped"


def run_state_from_result(result: dict, output: str | None = None) -> RunState:
    return RunState(
        status=classify_run_status(result),
        stop_reason=str(result.get("stop_reason") or ""),
        execution_completed=bool(result.get("execution_completed", False)),
        goal_completed=bool(result.get("goal_completed", False)),
        goal_status=str(result.get("goal_status") or "incomplete"),
        output=output,
    )


def sync_context_run_state(
    context: PolicyContext,
    result: dict,
    output: str | None = None,
) -> None:
    context.run = run_state_from_result(result, output=output)


def write_final_run_state(context_path: Path, result: dict, output: str) -> None:
    """Patch final run state without reloading PolicyContext.

    PolicyContext round-tripping can fail on platform-specific action subclasses in
    old turn records. Keep this as the only raw JSON patch site for final run state.
    """
    raw = json.loads(context_path.read_text(encoding="utf-8"))
    run_state = run_state_from_result(result, output)
    existing_run = raw.get("run") if isinstance(raw.get("run"), dict) else {}
    raw["run"] = {
        **existing_run,
        **run_state.model_dump(mode="json"),
    }
    for key in (
        "output", "stop_reason", "run_status", "execution_completed",
        "goal_completed", "goal_status",
    ):
        raw.pop(key, None)
    context_path.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
    )
