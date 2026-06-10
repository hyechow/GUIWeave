"""Action policy interface + the shared vision-LLM ``decide()`` template.

The three platform policies shared ~40 near-identical lines of LLM machinery
(resolve config → prep image → build messages → invoke_structured → post-process).
``BaseActionPolicy.decide`` is that template; a platform subclass only sets
``SYSTEM_PROMPT`` + ``decision_schema`` and may override the small hooks
(``_prepare_png`` / ``_build_user_text`` / ``_postprocess``). Passing the platform's
own ``decision_schema`` to ``invoke_structured`` is what isolates each platform's
action vocabulary in the LLM prompt (no cross-platform leak).
"""

from __future__ import annotations

import base64
import io
from typing import Optional, Protocol

from PIL import Image

from gui_agent.core.schemas import BaseActionDecision, Observation


class ActionPolicy(Protocol):
    """Stateless policy: maps one screenshot + one instruction to one action."""

    name: str

    def decide(
        self,
        observation: Observation,
        instruction: str,
        *,
        direction: Optional[str] = None,
        drag_column: Optional[str] = None,
        drag_steps: Optional[int] = None,
    ) -> BaseActionDecision:
        """Return the best action for the current observation and instruction."""


class BaseActionPolicy:
    """Shared vision-LLM ``decide()`` template + small per-platform hooks."""

    name = "base"
    SYSTEM_PROMPT: str = ""
    # The platform's own ActionDecision subclass — injected into the LLM prompt by
    # invoke_structured, so only this platform's action vocabulary is exposed.
    decision_schema: type[BaseActionDecision] = BaseActionDecision

    def decide(
        self,
        observation: Observation,
        instruction: str,
        *,
        direction: Optional[str] = None,
        drag_column: Optional[str] = None,
        drag_steps: Optional[int] = None,
        verbose: bool = True,
    ) -> BaseActionDecision:
        # Lazy imports keep ``import core.policies.base`` light (no eager langchain).
        from langchain_core.messages import HumanMessage, SystemMessage
        from langchain_openai import ChatOpenAI

        from gui_agent.core.config import resolve_llm_config
        from llm.structured import invoke_structured

        cfg = resolve_llm_config("action_policy")
        if verbose:
            print(f"Provider : {cfg.provider}")
            print(f"Model    : {cfg.model}")

        b64 = base64.b64encode(self._prepare_png(observation.png_bytes)).decode()
        llm = ChatOpenAI(model=cfg.model, api_key=cfg.api_key, base_url=cfg.base_url)
        user_text = self._build_user_text(
            instruction, direction=direction, drag_column=drag_column, drag_steps=drag_steps
        )
        messages = [
            SystemMessage(content=self.SYSTEM_PROMPT),
            HumanMessage(
                content=[
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ]
            ),
        ]
        decision = invoke_structured(llm, messages, self.decision_schema)
        return self._postprocess(
            decision, instruction, direction=direction, drag_column=drag_column, drag_steps=drag_steps
        )

    # --- hooks (defaults are vision-only, no hints, no post-processing) ---
    def _prepare_png(self, png_bytes: bytes) -> bytes:
        """Image prep before base64 (default: send raw). e.g. iphone halves Retina."""
        return png_bytes

    def _build_user_text(
        self,
        instruction: str,
        *,
        direction: Optional[str] = None,
        drag_column: Optional[str] = None,
        drag_steps: Optional[int] = None,
    ) -> str:
        """User-message text (default: just the instruction; iphone prepends picker hints)."""
        return f"操作指令：{instruction}\n\n请根据截图执行该指令。"

    def _postprocess(
        self,
        decision: BaseActionDecision,
        instruction: str,
        *,
        direction: Optional[str] = None,
        drag_column: Optional[str] = None,
        drag_steps: Optional[int] = None,
    ) -> BaseActionDecision:
        """Adjust the raw decision (default: identity; iphone runs the picker fixers)."""
        return decision


def resize_to_logical_png(png_bytes: bytes) -> bytes:
    """Downsample Retina screenshots to logical pixels before sending to a vision model."""

    img = Image.open(io.BytesIO(png_bytes))
    logical_w, logical_h = img.width // 2, img.height // 2
    small = img.resize((logical_w, logical_h), Image.LANCZOS)
    buf = io.BytesIO()
    small.save(buf, format="PNG")
    return buf.getvalue()
