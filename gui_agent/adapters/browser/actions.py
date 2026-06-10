"""Browser action space: BaseAction + the browser-only ``navigate`` action / ``url``.

``navigate`` (load a URL) is browser chrome — the omnibox is invisible to the page
screenshot and unreachable by page-keyboard, so it can't be done via tap+type. It
lives here, not in core, so iphone/android never see it.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import SerializeAsAny, model_validator

from gui_agent.core.schemas import BaseAction, BaseActionDecision

BrowserActionType = Literal[
    "tap", "type", "clear_text", "press_enter", "scroll", "drag", "navigate", "stop",
]


class BrowserAction(BaseAction):
    """A browser action: shared base + ``navigate`` / ``url`` (no iphone picker, no home)."""

    action_type: BrowserActionType  # type: ignore[assignment]
    url: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _nav_description(cls, data: object) -> object:
        if (
            isinstance(data, dict)
            and not data.get("description")
            and data.get("action_type") == "navigate"
            and data.get("url")
        ):
            data = {**data, "description": f"导航到 {data['url']}"}
        return data

    @model_validator(mode="after")
    def _require_url_for_navigate(self) -> "BrowserAction":
        if self.action_type == "navigate" and not self.url:
            raise ValueError("navigate 动作必须填写 url 字段")
        return self


class BrowserActionDecision(BaseActionDecision):
    action: SerializeAsAny[BrowserAction]  # type: ignore[assignment]
