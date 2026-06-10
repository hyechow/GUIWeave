"""Android action space: BaseAction + android nav keys (home / back / recents).

Promotes the three nav keys to first-class LLM actions (previously only AndroidDevice
methods). No extra fields — adb input takes the same normalized coords as the shared
base. No iphone picker, no browser navigate.
"""

from __future__ import annotations

from typing import Literal

from pydantic import SerializeAsAny

from gui_agent.core.schemas import BaseAction, BaseActionDecision

AndroidActionType = Literal[
    "tap", "type", "clear_text", "press_enter", "scroll", "drag",
    "home", "back", "recents", "stop",
]


class AndroidAction(BaseAction):
    """An Android action: shared base + the three nav keys (home / back / recents)."""

    action_type: AndroidActionType  # type: ignore[assignment]


class AndroidActionDecision(BaseActionDecision):
    action: SerializeAsAny[AndroidAction]  # type: ignore[assignment]
