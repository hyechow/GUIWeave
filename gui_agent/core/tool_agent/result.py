"""Stable AgentResult and report-context projection for Tool Agent runs."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from gui_agent.core.runtime.result import AgentResult
from gui_agent.core.schemas import PolicyContext
from gui_agent.core.tool_agent.presentation import (
    PresentationResult,
    present_result,
    write_presentation_artifact,
)
from gui_agent.core.tool_agent.runtime import ToolAgentRuntime


_EFFECT_BY_TASK_TYPE = {
    "MUTATE": "mutation",
    "NAVIGATE": "ui_state",
    "RETRIEVE": "data",
}
_TASK_TYPE_BY_EFFECT = {
    effect: task_type for task_type, effect in _EFFECT_BY_TASK_TYPE.items()
}


def project_tool_agent_result(
    *,
    intent: str,
    run: object,
    log_dir: Path,
    knowledge_summary: dict | None,
    platform: str,
    fallback_task_type: str,
    presentation: object | None = None,
    raw_input: str | None = None,
    router: dict | None = None,
    app_router: dict | None = None,
) -> AgentResult:
    """Project a Tool Agent run onto stable result and report contracts."""

    normalized_fallback = fallback_task_type.strip().upper()
    if normalized_fallback not in _EFFECT_BY_TASK_TYPE:
        raise ValueError(f"unsupported fallback task type {fallback_task_type!r}")
    phase = str(getattr(run, "phase", "failed"))
    output_value = getattr(run, "output", None)
    effect = str(getattr(run, "effect", "") or "").strip()
    if effect not in _TASK_TYPE_BY_EFFECT:
        effect = _EFFECT_BY_TASK_TYPE[normalized_fallback]
    models = {
        role: getattr(run, f"{role}_model", "")
        for role in ("master", "worker", "perception")
    }
    result_ref = getattr(run, "result_ref", None)
    platform_time = dict(getattr(run, "platform_time", None) or {})
    platform_rejections = [
        {
            "status": feedback.get("status"),
            "url": feedback.get("url"),
            "message": feedback.get("message"),
        }
        for event in (getattr(run, "trace", None) or [])
        if isinstance(event, dict) and event.get("event") == "runtime_action"
        for feedback in (event.get("platform_feedback") or [])
        if isinstance(feedback, dict) and feedback.get("rejected") is True
    ]
    result = AgentResult(
        goal=intent,
        output=json.dumps(output_value, ensure_ascii=False),
        summary=str(getattr(run, "summary", "tool-agent ended")),
        phase="completed" if phase == "completed" else "failed",
        verification="confirmed" if phase == "completed" else None,
        task_type=_TASK_TYPE_BY_EFFECT[effect],
        orchestrator={
            "kind": "tool_agent",
            "effect": effect,
            "perception_mode": getattr(run, "perception_mode", ""),
            "result_ref": result_ref.model_dump(mode="json") if result_ref else None,
            "platform_rejections": platform_rejections,
            "models": models,
            "platform_time": platform_time,
            "app_router": app_router,
            "trace_path": str(log_dir / "tool_agent_trace.json"),
            "replay_path": str(log_dir / "tool_agent_replay.json"),
            "presentation_path": str(log_dir / "tool_agent_presentation.json"),
            "presentation": (
                {
                    "status": getattr(presentation, "status", ""),
                    "result_digest": getattr(presentation, "result_digest", ""),
                    "model": getattr(presentation, "model", ""),
                }
                if presentation is not None
                else None
            ),
        },
    )
    context = PolicyContext(
        goal=intent,
        supervisor_policy_name="tool_agent.master",
        action_policy_name="tool_agent.worker",
        platform=platform,
        raw_input=raw_input or intent,
        router=router,
    )
    context.knowledge = knowledge_summary
    context.platform_time = platform_time or None
    context.outcome = result.to_program_outcome()
    context.models = {
        **{f"tool_agent.{role}": model for role, model in models.items()},
        "tool_agent.presentation": getattr(presentation, "model", ""),
    }
    context.orchestrator = dict(result.orchestrator or {})
    context.reply = getattr(presentation, "reply", None)
    (log_dir / "context.json").write_text(
        context.model_dump_json(indent=2),
        encoding="utf-8",
    )
    return result


def execute_tool_agent(
    *,
    intent: str,
    bundle: object,
    session: object,
    log_dir: Path,
    perception_mode: str,
    max_turns: int,
    allow_multi_action: bool,
    fallback_task_type: str,
    knowledge_summary: dict | None,
    knowledge: str = "",
    worker_knowledge: str = "",
    access_context: str = "",
    page_url: str = "",
    page_title: str = "",
    hud: object | None = None,
    raw_input: str | None = None,
    router: dict | None = None,
    app_router: dict | None = None,
    stop_requested: Callable[[], bool] | None = None,
) -> tuple[AgentResult, PresentationResult]:
    """Run Tool Agent and persist its replay, presentation, and stable context."""

    runtime = ToolAgentRuntime(
        bundle=bundle,
        platform=session,
        log_dir=log_dir,
        perception_mode=perception_mode,
        max_turns=max_turns,
        allow_multi_action=allow_multi_action,
        status_cb=hud.update if hud else None,
        stop_requested=stop_requested,
    )
    if hud:
        hud.set_goal(intent)
        hud.update("Tool Agent · preparing Master program")
    run = runtime.run(
        intent,
        knowledge=knowledge,
        worker_knowledge=worker_knowledge,
        access_context=access_context,
        page_url=page_url,
        page_title=page_title,
    )
    try:
        replay = json.loads(
            (log_dir / "tool_agent_replay.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        replay = {"status": "unavailable"}
    if hud:
        hud.update("Presentation · rendering verified result")
    presentation = present_result(
        goal=intent,
        phase=run.phase,
        result=run.output,
        summary=run.summary,
        replay=replay,
    )
    write_presentation_artifact(log_dir, presentation)
    with (log_dir / "tool_agent.log").open("a", encoding="utf-8") as stream:
        stream.write(
            f"[Presentation] {presentation.status.upper()} · {presentation.reply}\n"
        )
    result = project_tool_agent_result(
        intent=intent,
        run=run,
        log_dir=log_dir,
        knowledge_summary=knowledge_summary,
        platform=str(getattr(bundle, "platform", "") or ""),
        fallback_task_type=fallback_task_type,
        presentation=presentation,
        raw_input=raw_input,
        router=router,
        app_router=app_router,
    )
    return result, presentation


__all__ = ["execute_tool_agent", "project_tool_agent_result"]
