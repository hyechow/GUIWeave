"""Conversation session management: routing, history, reply generation."""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from llm.structured import invoke_structured
from policy_expr.config import resolve_llm_config
from policy_expr.output import generate_reply  # re-exported for callers

__all__ = ["RouterResult", "generate_reply", "route_message", "format_session_history"]


# ── Router ─────────────────────────────────────────────────────────────────


class RouterResult(BaseModel):
    goal: str = Field(
        default="",
        description=(
            "生成一个自包含、可直接执行的任务目标。"
            "需要操控手机时填写，格式：「在[APP]中[操作]」。"
            "不需要操控手机或信息不足无法确定 APP 时留空。"
        ),
    )
    needs_clarification: bool = Field(
        default=False,
        description=(
            "用户意图需要操控手机，但缺少关键信息（未指定 APP、操作不明确），需要反问用户。"
            "仅当 goal 留空且用户确实想操作手机时为 true。"
        ),
    )
    clarification: str = Field(
        default="",
        description="needs_clarification=true 时，简要说明需要用户补充什么信息。",
    )


_ROUTER_SYSTEM = """\
你是 iPhone 自动化助手的意图路由器。根据用户指令和对话历史，判断意图并生成任务目标。

三种情况：
1. 需要操控手机，且信息充分 → 填写 goal，格式：「在[APP]中[操作]」
2. 需要操控手机，但缺少关键信息（未指定 APP、操作不明确）→ goal 留空，needs_clarification=true，clarification 说明需要补充什么
3. 不需要操控手机（闲聊、通用知识问答）→ goal 留空，needs_clarification=false
⚠️ 只要用户要查看/操作某个 APP 内的数据（如账单、聊天记录、订单、余额），就必须操控手机，属于情况1。
不要因为「查询」「看看」「多少钱」等词就误判为通用问答——如果信息来源是某个 APP，就是手机操作。

goal 生成规则：
- 单 APP 任务格式：「在[APP]中[操作]」，必须包含明确的目标 APP 名称和具体操作
  例外：如果指令明确只要求打开 APP（含"打开"/"进入"等动词，如"打开微信"），直接复述即可
  ⚠️ 用户仅说了 APP 名称、没有任何动作（如只说"美团""微信"）→ needs_clarification=true，询问想做什么
- 跨 APP 任务（涉及多个 APP 的完整流程，如"从拼多多分享商品到微信"）：用自然语言完整描述，所有涉及的 APP 都必须明确提到，不要缩减为单 APP 格式
  例：「在拼多多中找到收藏的商品，通过分享功能发送给微信好友」
  例：「在支付宝中截图并通过微信发给张三」
- ⚠️ 用户明确提到的 APP 名称必须原样写入 goal，禁止替换为其他 APP（如用户说"记到文件传输助手"→ goal 必须用「文件传输助手」，不能改成「提醒事项」或其他）
- 不要猜测用户没提到的 APP（如用户说"点个外卖"但没说哪个 app → needs_clarification=true）
- 承接上文时，从历史中推断 APP 和操作上下文
  例：上文"打开微信"，用户说"发消息给李四"→ goal="在微信中给李四发送消息"
  例：上文"在美团点一杯咖啡"，用户说"换成奶茶"→ goal="在美团中点一杯奶茶"
- ⚠️ 用户提到的附带内容（要发送的文字、备注、附言等）必须原样保留在 goal 中，不能丢弃
  例：「分享给老Be，这个性价比高」→ goal 必须包含「附言：这个性价比高」
  例：「发消息给张三说明天见」→ goal 必须包含「说明天见」
- 不添加用户未提到的多余信息
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


# ── Router & reply ─────────────────────────────────────────────────────────


def route_message(
    user_msg: str,
    session: list[dict],
    prefs_context: str = "",
) -> RouterResult:
    llm = _get_llm()
    system = _ROUTER_SYSTEM
    if prefs_context:
        system += (
            "\n重要规则：以下偏好由用户设定，当用户未指定 APP 时优先使用偏好中的 APP，不要反问。"
            "但如果用户在指令中明确指定了某个 APP，以用户指令为准，忽略偏好。\n"
            + prefs_context + "\n"
        )
    history_text = format_session_history(session) if session else "（无历史）"
    messages = [
        SystemMessage(content=system),
        HumanMessage(content=f"对话历史：\n{history_text}\n\n当前用户指令：{user_msg}"),
    ]
    return invoke_structured(llm, messages, RouterResult)


