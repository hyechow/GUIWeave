"""Android action space: BaseAction + android nav keys (home / back / app_switch).

Promotes the three nav keys to first-class LLM actions (previously only AndroidDevice
methods). ``app_switch`` is KEYCODE_APP_SWITCH (the recents / multitask view). No extra
fields — adb input takes the same normalized coords as the shared base. No iphone
picker, no browser navigate.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import SerializeAsAny

from gui_agent.core.schemas import BaseAction, BaseActionDecision

AndroidActionType = Literal[
    "tap", "type", "clear_text", "press_enter", "scroll", "drag",
    "home", "back", "app_switch",
]


class AndroidAction(BaseAction):
    """An Android action: shared base + the three nav keys (home / back / app_switch)."""

    action_type: AndroidActionType  # type: ignore[assignment]


class AndroidActionDecision(BaseActionDecision):
    action: Optional[SerializeAsAny[AndroidAction]]  # type: ignore[assignment]
