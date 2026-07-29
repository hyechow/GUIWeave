"""Android action space: BaseAction + android nav keys (home / back / app_switch).

Promotes the three nav keys to first-class LLM actions (previously only AndroidDevice
methods). ``app_switch`` is KEYCODE_APP_SWITCH (the recents / multitask view). No extra
fields — adb input takes the same normalized coords as the shared base. No iphone
picker, no browser navigate.
"""

from __future__ import annotations

from typing import Literal

from pydantic import SerializeAsAny, model_validator

from gui_agent.core.schemas import BaseAction, BaseActionDecision

AndroidActionType = Literal[
    "tap", "type", "clear_text", "press_enter", "scroll", "drag",
    "home", "back", "app_switch",
]


class AndroidAction(BaseAction):
    """An Android action: shared base + the three nav keys (home / back / app_switch)."""

    action_type: AndroidActionType  # type: ignore[assignment]

    @model_validator(mode="after")
    def _drag_requires_endpoints(self) -> "AndroidAction":
        """Android drag is an explicit point-to-point touch gesture.

        Unlike the iPhone picker adapter, Android has no executor-side gesture
        synthesizer for a semantic direction/amount pair.  Reject an incomplete
        model action at the schema boundary instead of accepting it and failing
        later in the executor.
        """
        if self.action_type == "drag":
            missing = [
                name
                for name in ("x", "y", "to_x", "to_y")
                if getattr(self, name) is None
            ]
            if missing:
                raise ValueError(
                    "Android drag 动作必须填写完整 x/y/to_x/to_y；"
                    f"缺少 {', '.join(missing)}"
                )
        return self


class AndroidActionDecision(BaseActionDecision):
    action: SerializeAsAny[AndroidAction]  # type: ignore[assignment]
