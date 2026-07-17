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
from collections.abc import Callable, MutableSequence
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
        action_family: str = "",
        target_control: str = "",
        target_value: str = "",
        expected_result: str = "",
        evidence_context: str = "",
        context_reports: MutableSequence[dict] | Callable[[dict], None] | None = None,
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
        action_family: str = "",
        target_control: str = "",
        target_value: str = "",
        expected_result: str = "",
        evidence_context: str = "",
        verbose: bool = True,
        context_reports: MutableSequence[dict] | Callable[[dict], None] | None = None,
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

        prepared_png = self._prepare_png(observation.png_bytes)
        b64 = base64.b64encode(prepared_png).decode()
        llm = ChatOpenAI(model=cfg.model, api_key=cfg.api_key, base_url=cfg.base_url)
        instruction_text = self._build_user_text(
            instruction, direction=direction, drag_column=drag_column, drag_steps=drag_steps
        )
        semantic_context = _format_semantic_action_context(
            action_family=action_family,
            target_control=target_control,
            target_value=target_value,
            expected_result=expected_result,
        )
        if semantic_context:
            instruction_text = f"{instruction_text}\n\n{semantic_context}"
        user_text = instruction_text
        if evidence_context.strip():
            user_text = f"{user_text}\n\n{evidence_context.strip()}"
        messages = [
            SystemMessage(content=self.SYSTEM_PROMPT),
            HumanMessage(
                content=[
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ]
            ),
        ]
        if context_reports is not None:
            _append_report(context_reports, {
                "kind": "prompt_snapshot",
                "label": "action_policy",
                "roles": [
                    {
                        "role": "system",
                        "parts": [{
                            "label": "task_prompt",
                            "source_type": "prompt_asset",
                            "source": f"{self.name}.SYSTEM_PROMPT",
                            "type": "text",
                            "text": self.SYSTEM_PROMPT,
                            "chars": len(self.SYSTEM_PROMPT),
                        }],
                    },
                    {
                        "role": "human",
                        "parts": [
                            {
                                "label": "instruction",
                                "source_type": "runtime_state",
                                "source": "action_policy.instruction",
                                "type": "text",
                                "text": instruction_text,
                                "chars": len(instruction_text),
                            },
                            *([{
                                "label": "structured_evidence",
                                "source_type": "runtime_state",
                                "source": "action_policy.evidence_context",
                                "type": "text",
                                "text": evidence_context,
                                "chars": len(evidence_context),
                            }] if evidence_context.strip() else []),
                            {
                                "label": "screenshot",
                                "source_type": "image",
                                "source": "observation",
                                "type": "image",
                                "text": (
                                    f"[image_url omitted: image/png, {len(prepared_png)} bytes]"
                                ),
                                "chars": len(
                                    f"[image_url omitted: image/png, {len(prepared_png)} bytes]"
                                ),
                            },
                        ],
                    },
                ],
            })
        decision = invoke_structured(
            llm,
            messages,
            self.decision_schema,
            trace_sink=context_reports,
            trace_label="action_policy",
        )
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


def _format_semantic_action_context(
    *,
    action_family: str = "",
    target_control: str = "",
    target_value: str = "",
    expected_result: str = "",
) -> str:
    """Render the one-frame semantic contract without introducing another state object."""
    fields = [
        ("operation", action_family),
        ("target", target_control),
        ("value", target_value),
        ("expected visible result", expected_result),
    ]
    lines = [f"- {label}: {value}" for label, value in fields if str(value).strip()]
    if not lines:
        return ""
    return (
        "语义执行约束（用于从截图定位和消歧，不得改变指令目标，也不代表任务已完成）：\n"
        + "\n".join(lines)
    )


def _append_report(
    sink: MutableSequence[dict] | Callable[[dict], None],
    report: dict,
) -> None:
    if callable(sink):
        sink(report)
    else:
        sink.append(report)
