"""iPhone action space: BaseAction + iphone-specific actions/fields.

Extends the neutral ``BaseAction`` with iphone's picker vocabulary so the policy
injects ONLY iphone's action space into the LLM (no navigate/url leak). The picker
fields (value_direction / method / picker_* target_area) live here, not in core.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import SerializeAsAny, model_validator

from gui_agent.core.schemas import BaseAction, BaseActionDecision

ValueDirection = Literal["increase", "decrease"]
IPhoneScrollMethod = Literal["auto", "wheel", "drag"]
IPhoneScrollTargetArea = Literal[
    "main_content",
    "left_panel",
    "right_panel",
    "top_content",
    "bottom_content",
    "picker_left",
    "picker_center",
    "picker_right",
]
IPhoneActionType = Literal[
    "tap", "type", "clear_text", "press_enter", "scroll", "drag",
    "home", "kill_frontmost_app", "stop",
]


class IPhoneAction(BaseAction):
    """An iPhone action: shared base + picker fields + home / kill_frontmost_app."""

    action_type: IPhoneActionType  # type: ignore[assignment]
    target_area: IPhoneScrollTargetArea = "main_content"  # type: ignore[assignment]  # adds picker_*
    value_direction: Optional[ValueDirection] = None
    method: IPhoneScrollMethod = "auto"

    @model_validator(mode="after")
    def _scroll_drag_needs_dir(self) -> "IPhoneAction":
        if self.action_type in {"scroll", "drag"} and not (self.direction or self.value_direction):
            raise ValueError("scroll/drag 动作必须填写 direction 或 value_direction")
        return self


class IPhoneActionDecision(BaseActionDecision):
    action: SerializeAsAny[IPhoneAction]  # type: ignore[assignment]
