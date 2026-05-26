"""User preference memory: APP preferences, contacts, etc."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from llm.structured import invoke_structured
from policy_expr.config import resolve_llm_config

PREFS_PATH = Path(__file__).resolve().parent.parent / "data" / "user_preferences.json"


# ── Data models ─────────────────────────────────────────────────────────────


class AppPreference(BaseModel):
    intent: str
    app: str
    source: str  # "auto" | "manual"
    created_at: str = ""
    updated_at: str = ""


class ContactPreference(BaseModel):
    name: str
    app: str
    source: str
    created_at: str = ""
    updated_at: str = ""


class PreferenceStore(BaseModel):
    app_preferences: list[AppPreference] = Field(default_factory=list)
    contacts: list[ContactPreference] = Field(default_factory=list)


class ExtractedPreference(BaseModel):
    intent: str = Field(default="", description="意图类别：外卖/打车/咖啡/网购/酒店/快递/消息。无法归类则空")
    app: str = Field(default="", description="使用的APP名称")
    contacts: list[str] = Field(default_factory=list, description="涉及的联系人姓名")


# ── LLM extraction ──────────────────────────────────────────────────────────


def extract_prefs_llm(user_msg: str, goal: str, session: list[dict]) -> ExtractedPreference:
    from policy_expr.chat_session import format_session_history

    cfg = resolve_llm_config("router")
    llm = ChatOpenAI(model=cfg.model, api_key=cfg.api_key, base_url=cfg.base_url)
    history = format_session_history(session)
    return invoke_structured(llm, [
        SystemMessage(content=(
            "从用户的一次成功手机操作中提取偏好信息。\n"
            "intent: 意图类别，可选：外卖、打车、咖啡、网购、酒店、快递、消息。无法归类则空\n"
            "app: 使用的APP名称\n"
            "contacts: 涉及的联系人姓名列表，没有则空列表"
        )),
        HumanMessage(content=(
            f"对话历史：\n{history}\n\n"
            f"当前用户说：{user_msg}\n"
            f"执行目标：{goal}\n"
            f"结果：成功"
        )),
    ], ExtractedPreference)


# ── PreferenceManager ───────────────────────────────────────────────────────


class PreferenceManager:
    def __init__(self, path: Path = PREFS_PATH):
        self._path = path
        self._store: PreferenceStore = self._load()

    # -- Lookup --

    def lookup_app(self, intent: str) -> str | None:
        for p in self._store.app_preferences:
            if p.intent == intent:
                return p.app
        return None

    def format_prefs_for_prompt(self) -> str:
        if not self._store.app_preferences:
            return ""
        parts = [f"{p.intent}→{p.app}" for p in self._store.app_preferences]
        return "用户偏好（直接使用，不要反问）：\n" + ", ".join(parts)

    # -- Mutation --

    def set_app_pref(self, intent: str, app: str, source: str = "manual") -> None:
        now = _now_iso()
        for p in self._store.app_preferences:
            if p.intent == intent:
                if p.source == "manual" and source == "auto":
                    return  # manual 优先，auto 不覆盖
                p.app = app
                p.source = source
                p.updated_at = now
                self._save()
                return
        self._store.app_preferences.append(AppPreference(
            intent=intent, app=app, source=source,
            created_at=now, updated_at=now,
        ))
        self._save()

    def remove_app_pref(self, intent: str) -> bool:
        before = len(self._store.app_preferences)
        self._store.app_preferences = [
            p for p in self._store.app_preferences if p.intent != intent
        ]
        if len(self._store.app_preferences) < before:
            self._save()
            return True
        return False

    def add_contact(self, name: str, app: str, source: str = "auto") -> None:
        now = _now_iso()
        for c in self._store.contacts:
            if c.name == name:
                if c.source == "manual" and source == "auto":
                    return
                c.app = app
                c.source = source
                c.updated_at = now
                self._save()
                return
        self._store.contacts.append(ContactPreference(
            name=name, app=app, source=source,
            created_at=now, updated_at=now,
        ))
        self._save()

    # -- Listing --

    def list_app_prefs(self) -> list[AppPreference]:
        return list(self._store.app_preferences)

    # -- Auto-extract from successful execution --

    def auto_extract(self, user_msg: str, goal: str, session: list[dict]) -> None:
        try:
            extracted = extract_prefs_llm(user_msg, goal, session)
        except Exception:
            return

        if extracted.intent and extracted.app:
            self.set_app_pref(extracted.intent, extracted.app, source="auto")
        for name in extracted.contacts:
            if extracted.app:
                self.add_contact(name, extracted.app, source="auto")

    # -- Persistence --

    def _load(self) -> PreferenceStore:
        if self._path.exists():
            try:
                return PreferenceStore.model_validate_json(
                    self._path.read_text(encoding="utf-8")
                )
            except Exception:
                pass
        return PreferenceStore()

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = self._store.model_dump_json(indent=2)
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
        try:
            os.write(fd, data.encode("utf-8"))
            os.close(fd)
            os.rename(tmp, self._path)
        except Exception:
            os.close(fd)
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
