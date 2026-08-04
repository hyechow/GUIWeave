"""Conversation session management: routing, history, reply generation."""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from llm.structured import invoke_structured
from gui_agent.core.config import resolve_llm_config
from gui_agent.core.llm.output import generate_reply  # re-exported for callers
from gui_agent.core.llm.temporal import resolve_temporal_expressions

__all__ = ["RouterResult", "generate_reply", "route_message", "format_session_history"]


# ── Router ─────────────────────────────────────────────────────────────────


class RouterResult(BaseModel):
    goal: str = Field(
        default="",
        description=(
            "A self-contained, directly executable task goal. Fill this when device "
            "interaction is required, naming the application, target, and requested "
            "operation. When a time range qualifies an extreme or ranking metric, attach "
            "the actual range directly to that metric without changing its granularity "
            "or rewriting resolved date endpoints. Leave this empty when no device "
            "interaction is needed or essential routing information is genuinely missing."
        ),
    )
    needs_clarification: bool = Field(
        default=False,
        description=(
            "True only when the user wants device interaction but essential routing "
            "information is missing. It must be false whenever goal is non-empty."
        ),
    )
    clarification: str = Field(
        default="",
        description=(
            "When needs_clarification is true, briefly state what information the user "
            "must provide, using the language of the current user instruction."
        ),
    )


def _router_system_for(platform: str) -> tuple[str, str | None]:
    """Select the platform's router system prompt + optional known-apps rule template.
    The router FRAMEWORK is neutral; the prompt is platform-specific (iphone/android:
    操控手机/APP; browser: 网页任务). The known-apps rule is also per-platform: only platforms whose
    entry point is an address the router can't see (browser: URL) provide a template —
    on iphone the app name IS the entry, and injecting the list there disturbed
    prefs/context-carry behavior (4/53 eval regressions). Lazy import keeps
    chat_session a leaf — adapters are pulled only at route time."""
    if platform == "browser":
        from gui_agent.adapters.browser.router_prompt import (
            BROWSER_KNOWN_APPS_RULE,
            BROWSER_ROUTER_SYSTEM,
        )
        system = BROWSER_ROUTER_SYSTEM
        known_apps_rule = BROWSER_KNOWN_APPS_RULE
    elif platform == "android":
        from gui_agent.adapters.android.router_prompt import ANDROID_ROUTER_SYSTEM

        system = ANDROID_ROUTER_SYSTEM
        known_apps_rule = None
    else:
        from gui_agent.adapters.iphone.router_prompt import IPHONE_ROUTER_SYSTEM

        system = IPHONE_ROUTER_SYSTEM
        known_apps_rule = None
    from gui_agent.prompts import load_prompt_text

    shared_goal_rules = load_prompt_text("context.router.shared_goal")
    return f"{system}\n\n{shared_goal_rules}", known_apps_rule


def _get_llm() -> ChatOpenAI:
    cfg = resolve_llm_config("router")
    return ChatOpenAI(model=cfg.model, api_key=cfg.api_key, base_url=cfg.base_url)


# ── History helpers ────────────────────────────────────────────────────────


def format_session_history(history: list[dict]) -> str:
    if not history:
        return "（无历史）"
    lines = []
    for i, entry in enumerate(history, 1):
        status = (
            "✓"
            if entry.get("phase") == "completed"
            and entry.get("verification") == "confirmed"
            else "~"
            if entry.get("phase") == "completed"
            else "✗"
        )
        output = (
            entry.get("output")
            or entry.get("result_summary")
            or entry.get("summary")
            or entry.get("reply")
            or ""
        )
        lines.append(f"{i}. 用户说「{entry['user_msg']}」→ {status} {output}")
    return "\n".join(lines)


def _mentioned_known_apps(
    user_msg: str,
    session: list[dict],
    prefs_context: str,
    known_apps: list[str],
) -> list[str]:
    """Known-site names explicitly present in current input, history, or prefs.

    The knowledge list is not a resolver for vague references like "our shop".
    Those references need conversation history or user preference memory; otherwise
    showing every known app to the router invites it to guess.
    """
    haystack_parts = [user_msg, prefs_context]
    for entry in session:
        for key in ("user_msg", "goal", "output", "reply"):
            value = entry.get(key)
            if isinstance(value, str):
                haystack_parts.append(value)
    haystack = "\n".join(haystack_parts).lower()
    return [app for app in known_apps if app.lower() in haystack]


# ── Router & reply ─────────────────────────────────────────────────────────


def route_message(
    user_msg: str,
    session: list[dict],
    prefs_context: str = "",
    platform: str = "iphone",
    current_url: str = "",
    current_title: str = "",
    current_site: str = "",
) -> RouterResult:
    llm = _get_llm()
    system, known_apps_rule = _router_system_for(platform)
    # Resolve temporal expressions so the router outputs absolute dates in the goal.
    resolved_msg = resolve_temporal_expressions(user_msg)
    # Knowledge-base awareness: the router runs BEFORE knowledge injection, so without
    # this it asks the user for facts (entry URL) that knowledge already holds and
    # orchestrator planning will inject. The rule TEXT is the platform's (adapter router_prompt);
    # core only discovers the app list and fills it in. Lazy import keeps chat_session
    # a leaf at import time.
    if known_apps_rule:
        from gui_agent.core.self_learning.app_summary import list_known_apps
        known_apps = list_known_apps(platform)
        mentioned_apps = _mentioned_known_apps(user_msg, session, prefs_context, known_apps)
        if mentioned_apps:
            system += known_apps_rule.format(apps="、".join(mentioned_apps))
    if prefs_context:
        system += (
            "\n重要规则：以下偏好由用户设定，当用户未指定 APP 时优先使用偏好中的 APP，不要反问。"
            "但如果用户在指令中明确指定了某个 APP，以用户指令为准，忽略偏好。\n"
            + prefs_context + "\n"
        )
    history_text = format_session_history(session) if session else "（无历史）"
    page_ctx = ""
    if current_url or current_site:
        # The front-tab identity the vision-only screenshot can't show (browser omnibox). Lead
        # with a semantic site name (matched from the url) + page title; the raw url (often an
        # IP like 192.168.31.57:22000) is opaque to the model, so it stays a ground-truth tail.
        _parts = []
        if current_site:
            _parts.append(f"站点：{current_site}（已知应用）")
        if current_title:
            _parts.append(f"页面：{current_title}")
        if current_url:
            _parts.append(f"url：{current_url}")
        page_ctx = "\n\n当前前台页面（ground truth，截图看不到地址栏）：" + " · ".join(_parts)
    messages = [
        SystemMessage(content=system),
        HumanMessage(content=f"对话历史：\n{history_text}\n\n当前用户指令：{resolved_msg}{page_ctx}"),
    ]
    return invoke_structured(llm, messages, RouterResult)
