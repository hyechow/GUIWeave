"""Conversation session management: routing, history, reply generation."""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from llm.structured import invoke_structured
from gui_agent.core.config import resolve_llm_config
from gui_agent.core.output import generate_reply  # re-exported for callers
from gui_agent.core.temporal import resolve_temporal_expressions

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


def _router_system_for(platform: str) -> tuple[str, str | None]:
    """Select the platform's router system prompt + optional known-apps rule template.
    The router FRAMEWORK is neutral; the prompt is platform-specific (iphone: 操控手机/APP;
    browser: 网页任务). The known-apps rule is also per-platform: only platforms whose
    entry point is an address the router can't see (browser: URL) provide a template —
    on iphone the app name IS the entry, and injecting the list there disturbed
    prefs/context-carry behavior (4/53 eval regressions). Lazy import keeps
    chat_session a leaf — adapters are pulled only at route time."""
    if platform == "browser":
        from gui_agent.adapters.browser.router_prompt import (
            BROWSER_KNOWN_APPS_RULE,
            BROWSER_ROUTER_SYSTEM,
        )
        return BROWSER_ROUTER_SYSTEM, BROWSER_KNOWN_APPS_RULE
    from gui_agent.adapters.iphone.router_prompt import IPHONE_ROUTER_SYSTEM
    return IPHONE_ROUTER_SYSTEM, None


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
    platform: str = "iphone",
) -> RouterResult:
    llm = _get_llm()
    system, known_apps_rule = _router_system_for(platform)
    # Resolve temporal expressions so the router outputs absolute dates in the goal.
    resolved_msg = resolve_temporal_expressions(user_msg)
    # Knowledge-base awareness: the router runs BEFORE knowledge injection, so without
    # this it asks the user for facts (entry URL) that knowledge already holds and
    # decompose will inject. The rule TEXT is the platform's (adapter router_prompt);
    # core only discovers the app list and fills it in. Lazy import keeps chat_session
    # a leaf at import time.
    if known_apps_rule:
        from gui_agent.core.self_learning.app_summary import list_known_apps
        known_apps = list_known_apps(platform)
        if known_apps:
            system += known_apps_rule.format(apps="、".join(known_apps))
    if prefs_context:
        system += (
            "\n重要规则：以下偏好由用户设定，当用户未指定 APP 时优先使用偏好中的 APP，不要反问。"
            "但如果用户在指令中明确指定了某个 APP，以用户指令为准，忽略偏好。\n"
            + prefs_context + "\n"
        )
    history_text = format_session_history(session) if session else "（无历史）"
    messages = [
        SystemMessage(content=system),
        HumanMessage(content=f"对话历史：\n{history_text}\n\n当前用户指令：{resolved_msg}"),
    ]
    return invoke_structured(llm, messages, RouterResult)


