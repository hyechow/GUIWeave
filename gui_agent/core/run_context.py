"""Context persistence helpers used by runner entrypoints."""

from __future__ import annotations

import json
from pathlib import Path

from gui_agent.core.schemas import PolicyContext


def save_context(path: Path, context: PolicyContext) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(context.model_dump_json(indent=2), encoding="utf-8")


def extract_checker(supervisor: object) -> dict | None:
    check = getattr(supervisor, "_last_check", None)
    if check is None:
        return None
    return check.model_dump(exclude_none=True)


def extract_plan(supervisor: object) -> dict | None:
    plan = getattr(supervisor, "_last_plan", None)
    if plan is None:
        return None
    return plan.model_dump(exclude_none=True)


def extract_replan(supervisor: object) -> dict | None:
    replan = getattr(supervisor, "_last_replan", None)
    if replan is None:
        return None
    return replan.model_dump(exclude_none=True)


def load_context(
    path: Path,
    prompt: str,
    supervisor_name: str,
    action_name: str,
    raw_input: str | None = None,
    router: dict | None = None,
) -> PolicyContext:
    if path.exists():
        return PolicyContext.model_validate(json.loads(path.read_text(encoding="utf-8")))
    return PolicyContext(
        goal=prompt,
        supervisor_policy_name=supervisor_name,
        action_policy_name=action_name,
        raw_input=raw_input if raw_input is not None else prompt,
        router=router,
    )
