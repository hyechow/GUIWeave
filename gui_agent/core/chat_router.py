"""Conversation-aware routing between text replies and GUI execution."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ConfigDict, Field, model_validator

from gui_agent.core.config import resolve_llm_config
from gui_agent.prompts import load_prompt_text
from llm.structured import invoke_structured


_URL_PATTERN = re.compile(
    r"(?<![\w@])(?:"
    r"(?:https?://|www\.)[^\s)\]}>'\",，。！？；：]+"
    r"|(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,63}"
    r"(?:/[^\s)\]}>'\",，。！？；：]*)?"
    r")",
    re.IGNORECASE,
)
_CJK_PATTERN = re.compile(r"[\u3400-\u9fff]")
_ZH_REASON = {
    "gui": "该请求需要读取或操作当前界面。",
    "respond": "该请求无需读取或操作界面。",
    "clarify": "执行 GUI 任务所需的信息不足。",
    "cancel": "用户要求中止正在执行的 GUI 任务。",
}
_DEFAULT_REASON = {
    "gui": "The request requires current GUI evidence or an interface action.",
    "respond": "The request does not require current GUI access.",
    "clarify": "Required GUI task details are missing.",
    "cancel": "The user asked to stop an active GUI task.",
}


ChatRouteName = Literal["gui", "respond", "clarify", "cancel"]


class ChatRoute(BaseModel):
    """One safe, structured decision for a user chat message."""

    model_config = ConfigDict(extra="forbid")

    route: ChatRouteName
    reply: str = Field(default="", max_length=4000)
    gui_goal: str = Field(default="", max_length=4000)
    cancel_task_id: str = Field(default="", max_length=100)
    reason: str = Field(default="", max_length=300)

    @model_validator(mode="after")
    def validate_payload(self) -> "ChatRoute":
        if not self.reason.strip():
            self.reason = _DEFAULT_REASON[self.route]
        if self.route == "gui" and not self.gui_goal.strip():
            raise ValueError("gui route requires gui_goal")
        if self.route in {"respond", "clarify"} and not self.reply.strip():
            raise ValueError(f"{self.route} route requires reply")
        return self


class ChatIntentRouter:
    """Use the configured text router to classify and answer one chat turn."""

    def __init__(
        self,
        llm: ChatOpenAI | None = None,
        *,
        invoke: Callable[..., ChatRoute] = invoke_structured,
    ) -> None:
        if llm is None:
            cfg = resolve_llm_config("router")
            llm = ChatOpenAI(
                model=cfg.model,
                api_key=cfg.api_key,
                base_url=cfg.base_url,
                timeout=cfg.timeout_s,
                max_retries=cfg.max_retries,
                temperature=0,
            )
        self._llm = llm
        self._invoke = invoke
        self._system = load_prompt_text("task.chat.router")

    def route(
        self,
        message: str,
        history: Sequence[dict[str, Any]],
        platform: str,
    ) -> ChatRoute:
        payload = {
            "platform": platform,
            "conversation": list(history)[-12:],
            "message": message,
        }
        route = self._invoke(
            self._llm,
            [
                SystemMessage(content=self._system),
                HumanMessage(content=json.dumps(payload, ensure_ascii=False)),
            ],
            ChatRoute,
            trace_label="chat.router",
        )
        recent = list(history)[-1:]
        user_messages = [
            str(turn.get("user", "")).strip()
            for turn in recent
            if turn.get("route") == "clarify" and str(turn.get("user", "")).strip()
        ]
        user_messages.append(message)
        grounded_goal = "；".join(user_messages)
        chinese_conversation = bool(_CJK_PATTERN.search(grounded_goal))
        if chinese_conversation:
            route = route.model_copy(update={
                "reason": route.reason if _CJK_PATTERN.search(route.reason)
                else _ZH_REASON[route.route],
                "gui_goal": grounded_goal
                if route.route == "gui" and not _CJK_PATTERN.search(route.gui_goal)
                else route.gui_goal,
            })
        if route.route != "gui":
            return route
        user_text = "\n".join(
            [message, *(str(turn.get("user", "")) for turn in history)]
        )
        if set(_URL_PATTERN.findall(route.gui_goal)) <= set(
            _URL_PATTERN.findall(user_text)
        ):
            return route
        return route.model_copy(update={"gui_goal": grounded_goal})


__all__ = ["ChatIntentRouter", "ChatRoute", "ChatRouteName"]
