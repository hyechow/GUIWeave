"""Structured read: extract a read milestone's `returns` fields off the result frame.

The orchestrator's read primitive. A read milestone's job is single-frame data extraction
(the inspect insight): read the requested fields, interpreting non-text signals (icons /
colour / position) via the app's acceptance cues (_check.md), and return {field: value}
(empty when not readable = 当没有 — never blocks the program). The interpreter then branches
on those structured values, so "the checker saw it but the output didn't know" goes away.

This runs ON the milestone's done-frame (the supervisor advanced the read milestone there,
i.e. when the result is visible), so the verdict is read at exactly the right moment.
"""

from __future__ import annotations

import base64
from collections.abc import Callable

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from gui_agent.context import ContextBlock
from gui_agent.context.runtime import render_prompt_context
from gui_agent.core.config import resolve_llm_config
from gui_agent.prompts import load_prompt_text
from llm.structured import invoke_structured

_SYSTEM = load_prompt_text("task.orchestrator.structured_read")


class _FieldRead(BaseModel):
    field: str = Field(description="字段名（照抄请求里的字段）")
    evidence: str = Field(default="", description="界面上与该字段相关的信号（文字/图标/颜色/位置），先写这个")
    value: str = Field(default="", description="据 evidence + 判读提示得出的字段文字值；图标/颜色也要判成文字，确实没有才留空")


class _StructuredRead(BaseModel):
    reads: list[_FieldRead] = Field(default_factory=list)


def structured_read(
    png_bytes: bytes,
    returns: list[str],
    read_spec: str = "",
    check_knowledge: str = "",
    prepare_vision_prompt_png: Callable[[bytes], bytes] | None = None,
) -> dict[str, str]:
    """Read `returns` fields off the frame -> {field: value} (empty when not readable).

    `read_spec` is the task-level read instruction the decomposer generated from the user goal
    (what each field means + how to judge it off the UI) — the PRIMARY judgment guidance, so the
    extraction semantics live in the program, not in this hardcoded prompt. `check_knowledge`
    (the app's _check.md signal conventions) is a supplementary reference for icon/colour cues.
    `prepare_vision_prompt_png` is supplied by the platform bundle; shared code must not assume
    iPhone Retina geometry for browser/android observations."""
    if not returns:
        return {}
    cfg = resolve_llm_config("reader")
    llm = ChatOpenAI(
        model=cfg.model, api_key=cfg.api_key, base_url=cfg.base_url,
        extra_body={"enable_thinking": False},
    )
    text = render_prompt_context([
        ContextBlock(
            id="runtime.read.requested_fields",
            source_type="runtime_state",
            source="orchestrator.read",
            ttl="turn",
            priority=20,
            content=f"读取以下字段的当前值：{'、'.join(returns)}。",
        ),
        ContextBlock(
            id="runtime.read.spec",
            source_type="runtime_state",
            source="program.read_spec",
            ttl="task",
            priority=25,
            content="【读取说明】（任务定义，按此判读每个字段；优先于下方应用约定）：\n" + read_spec,
        ) if read_spec else None,
        ContextBlock(
            id="knowledge.check_rules",
            source_type="knowledge_base",
            source="knowledge_base",
            ttl="session",
            priority=50,
            content="【界面信号参考】（应用约定，某字段若以图标/颜色/位置表示可据此判读成文字值）：\n" + check_knowledge,
        ) if check_knowledge else None,
    ])
    prepare_png = prepare_vision_prompt_png or (lambda b: b)
    b64 = base64.b64encode(prepare_png(png_bytes)).decode()
    messages = [
        SystemMessage(content=_SYSTEM),
        HumanMessage(content=[
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]),
    ]
    result = invoke_structured(llm, messages, _StructuredRead)
    # Keep only requested fields; default any missing to "" (当没有).
    by_field = {fr.field: (fr.value or "") for fr in result.reads}
    return {f: by_field.get(f, "") for f in returns}
