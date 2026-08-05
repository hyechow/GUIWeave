"""Add one semantic clarification to a user task before orchestration."""

from __future__ import annotations

from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from gui_agent.core.config import resolve_llm_config
from gui_agent.prompts import load_prompt_text
from llm.structured import invoke_structured

_SYSTEM = load_prompt_text("task.router.intent_resolver")


class IntentResolution(BaseModel):
    """An optional semantic delta that supplements, but never replaces, the source task."""

    semantic_supplement: str = Field(
        default="",
        description=(
            "One short application-agnostic clarification of implicit or ambiguous meaning; "
            "empty when the original task is already semantically complete"
        ),
    )


def _llm() -> ChatOpenAI:
    cfg = resolve_llm_config("supervisor.intent")
    if not cfg.model:
        cfg = resolve_llm_config("supervisor")
    return ChatOpenAI(
        model=cfg.model,
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        temperature=0,
    )


def resolve_intent(
    goal: str,
    *,
    llm: Optional[ChatOpenAI] = None,
    trace_sink: Optional[list[dict]] = None,
) -> IntentResolution:
    """Return only meaning that must be added to the original task."""
    source_goal = goal.strip()
    if not source_goal:
        return IntentResolution()
    resolution = invoke_structured(
        llm or _llm(),
        [
            SystemMessage(content=_SYSTEM),
            HumanMessage(content=f"Original task:\n{source_goal}"),
        ],
        IntentResolution,
        trace_sink=trace_sink,
        trace_label="orchestrator.intent",
    )
    return IntentResolution(
        semantic_supplement=resolution.semantic_supplement.strip(),
    )
