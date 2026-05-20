"""Conversation session management: routing, history, reply generation."""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from llm.structured import invoke_structured
from policy_expr.config import resolve_llm_config
from policy_expr.output import generate_reply  # re-exported for callers

__all__ = ["RouterResult", "generate_reply", "route_message", "format_session_history", "build_goal_with_history"]


# ── Router ─────────────────────────────────────────────────────────────────


class RouterResult(BaseModel):
    actionable: bool = Field(description="该指令是否需要操控手机才能完成")
    reason: str = Field(default="", description="分类理由，供回复生成参考")


_ROUTER_SYSTEM = """\
你是 iPhone 自动化助手的意图分类器。只判断用户的指令是否需要通过操控手机来完成，不生成回复。

分类标准：
- 只要是关于手机上 app 的操作或信息获取 → actionable=true（包括但不限于打开 app、点击、输入、发送、截图查看内容等）
- 不涉及手机操作：询问历史记录、询问自身身份/能力、闲聊等 → actionable=false，reason 简述原因
- 边界模糊时倾向放行（actionable=true）
"""

def _get_llm() -> ChatOpenAI:
    cfg = resolve_llm_config("router")
    return ChatOpenAI(model=cfg.model, api_key=cfg.api_key, base_url=cfg.base_url)


# ── History helpers ────────────────────────────────────────────────────────


def format_session_history(history: list[dict]) -> str:
    if not history:
        return "（无历史）"
    lines = []
    for i, entry in enumerate(history, 1):
        status = "✓" if entry.get("goal_completed") else "✗"
        lines.append(f"{i}. 用户说「{entry['user_msg']}」→ {status} {entry['result_summary']}")
    return "\n".join(lines)


def build_goal_with_history(user_msg: str, session: list[dict]) -> str:
    if not session:
        return user_msg
    return f"之前的对话历史：\n{format_session_history(session)}\n\n当前用户指令：{user_msg}"


# ── Router & reply ─────────────────────────────────────────────────────────


def route_message(user_msg: str, session: list[dict]) -> RouterResult:
    llm = _get_llm()
    history_text = format_session_history(session) if session else "（无历史）"
    messages = [
        SystemMessage(content=_ROUTER_SYSTEM),
        HumanMessage(content=f"对话历史：\n{history_text}\n\n当前用户指令：{user_msg}"),
    ]
    return invoke_structured(llm, messages, RouterResult)


