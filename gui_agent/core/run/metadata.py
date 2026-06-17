"""Per-turn context metadata synchronization."""

from __future__ import annotations

import json
from typing import Callable

from gui_agent.core.config import resolve_llm_config
from gui_agent.core.schemas import PolicyContext, SupervisorStep

MODEL_KEYS = (
    "supervisor",
    "supervisor.decompose",
    "action_policy",
    "reader",
    "output",
    "router",
    "back_nav",
)


def sync_turn_metadata(
    *,
    context: PolicyContext,
    supervisor,
    sv_step: SupervisorStep,
    program,
    say: Callable[[str], None],
) -> None:
    """Persist model, milestone, task type, and collection-scope metadata."""
    if not context.models:
        for key in MODEL_KEYS:
            try:
                context.models[key] = resolve_llm_config(key).model or ""
            except Exception:
                pass

    if program is None and not context.milestones and hasattr(supervisor, "_milestones"):
        context.milestones = [
            {
                "id": milestone.id,
                "name": milestone.name,
                "description": milestone.description,
                "kind": milestone.kind,
                "success_condition": milestone.success_condition,
            }
            for milestone in supervisor._milestones.values()
        ]

    if hasattr(supervisor, "task_type") and context.task_type is None:
        context.task_type = supervisor.task_type
        say(f"任务类型: {context.task_type}")

    if sv_step.collection_scope and sv_step.collection_scope != context.collection_scope:
        context.collection_scope = sv_step.collection_scope
        scope = json.dumps(context.collection_scope.model_dump(exclude_none=True), ensure_ascii=False)
        say("采集范围: " + scope)
