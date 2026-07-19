"""Independent, acquisition-only React fallback policy."""

from __future__ import annotations

import json
from typing import Literal

from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ConfigDict, Field, model_validator

from gui_agent.context import ContextBlock
from gui_agent.core.config import resolve_llm_config
from gui_agent.core.llm.messages import assemble_messages
from gui_agent.core.schemas import Observation
from gui_agent.prompts import load_prompt_text
from llm.structured import invoke_structured


class AcquireDecision(BaseModel):
    """A bounded proposal; Runtime validates and records every effect."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["move", "boundary", "blocked"]
    reason: str = Field(min_length=1)
    bound_hint: str = ""
    action_family: Literal[
        "bind_region", "paginate_next", "paginate_prev", "scroll_forward",
        "scroll_backward", "load_more", "wait",
    ] | None = None
    target_role: Literal["bound_region", "pager", "scroll_affordance"] | None = None
    instruction: str = ""

    @model_validator(mode="after")
    def _shape(self) -> "AcquireDecision":
        if self.kind == "move":
            if self.action_family is None or self.target_role is None:
                raise ValueError("move requires action_family and target_role")
            if self.action_family == "bind_region":
                if not self.bound_hint or self.target_role != "bound_region":
                    raise ValueError("bind_region requires bound_hint and bound_region role")
            elif not self.instruction.strip():
                raise ValueError("physical move requires instruction")
        elif self.action_family is not None or self.target_role is not None:
            raise ValueError("terminal acquisition proposal cannot carry an action")
        return self


_SYSTEM = load_prompt_text("task.statement.acquire")


def decide_acquisition(
    observation: Observation,
    context: dict,
    *,
    context_reports: list[dict] | None = None,
) -> AcquireDecision:
    cfg = resolve_llm_config("acquire")
    if not cfg.model:
        cfg = resolve_llm_config("supervisor")
    from llm.provider_config import dashscope_extra_body

    llm = ChatOpenAI(
        model=cfg.model,
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        extra_body=dashscope_extra_body(cfg.model),
    )
    block = ContextBlock(
        id="runtime.acquire_context",
        budget="required",
        source_type="runtime_state",
        source="acquire_memory_view",
        ttl="statement",
        priority=10,
        content="## AcquisitionContext\n" + json.dumps(context, ensure_ascii=False),
    )
    messages = assemble_messages(
        _SYSTEM,
        observation,
        human_blocks=[block],
        image_resize="none",
        label="statement.acquire",
        context_reports=context_reports,
        decision_text="只输出一个受限采集移动、视觉边界提议或 blocked。",
    )
    return invoke_structured(
        llm,
        messages,
        AcquireDecision,
        trace_sink=context_reports,
        trace_label="statement.acquire",
    )


__all__ = ["AcquireDecision", "decide_acquisition"]
