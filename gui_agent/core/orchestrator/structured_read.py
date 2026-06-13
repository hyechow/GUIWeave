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

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from gui_agent.core.config import resolve_llm_config
from gui_agent.core.policies.base import resize_to_logical_png
from llm.structured import invoke_structured

_SYSTEM = """\
你从截图读取【指定字段】的当前值，用于程序判断。对每个字段：
1. 先在 evidence 里写出你在界面上看到的、与该字段相关的具体信号——包括文字，也包括图标/颜色/形状/位置（例：「起点终点输入框之间右侧有一个绿色圆形对勾✓」）。
2. 再据【判读提示】把该信号判读成 value 的文字值。**图标/颜色信号必须判读成文字写进 value，不能因为它不是文字就留空**（如绿色✓→连通、红字「路径不可达」→不可达、灰色?→未检测）。
3. 确实读不到（界面没有该信息）才把 value 留空。
只读被指定的字段，不要补充其他字段、不要编造。
"""


class _FieldRead(BaseModel):
    field: str = Field(description="字段名（照抄请求里的字段）")
    evidence: str = Field(default="", description="界面上与该字段相关的信号（文字/图标/颜色/位置），先写这个")
    value: str = Field(default="", description="据 evidence + 判读提示得出的字段文字值；图标/颜色也要判成文字，确实没有才留空")


class _StructuredRead(BaseModel):
    reads: list[_FieldRead] = Field(default_factory=list)


def structured_read(png_bytes: bytes, returns: list[str], check_knowledge: str = "") -> dict[str, str]:
    """Read `returns` fields off the frame -> {field: value} (empty when not readable).
    `check_knowledge` (the app's _check.md acceptance cues) lets it judge visual signals."""
    if not returns:
        return {}
    cfg = resolve_llm_config("reader")
    llm = ChatOpenAI(
        model=cfg.model, api_key=cfg.api_key, base_url=cfg.base_url,
        extra_body={"enable_thinking": False},
    )
    text = f"读取以下字段的当前值：{'、'.join(returns)}。"
    if check_knowledge:
        text += ("\n判读提示（某字段若以图标/颜色/位置表示，按此判读成文字值）：\n" + check_knowledge)
    b64 = base64.b64encode(resize_to_logical_png(png_bytes)).decode()
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
