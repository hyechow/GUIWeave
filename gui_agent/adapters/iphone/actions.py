"""iPhone action schema for the shared Tool Agent runtime."""

from __future__ import annotations

from typing import Literal

from pydantic import SerializeAsAny

from gui_agent.core.schemas import BaseAction, BaseActionDecision


IPhoneActionType = Literal[
    "tap",
    "type",
    "clear_text",
    "press_enter",
    "scroll",
    "drag",
    "home",
    "app_switch",
]


class IPhoneAction(BaseAction):
    """Actions supported through the local iPhone Mirroring helpers."""

    action_type: IPhoneActionType  # type: ignore[assignment]


class IPhoneActionDecision(BaseActionDecision):
    action: SerializeAsAny[IPhoneAction]  # type: ignore[assignment]


__all__ = ["IPhoneAction", "IPhoneActionDecision"]

