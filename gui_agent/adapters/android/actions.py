"""Android action space: touch gestures, app launch, and Android navigation keys.

The adapter preserves the full task-level input surface already implemented by
``AndroidDevice`` while leaving lifecycle/diagnostic primitives (wake, raw keycode,
IME setup) internal.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, SerializeAsAny, model_validator

from gui_agent.core.schemas import BaseAction, BaseActionDecision

AndroidActionType = Literal[
    "tap", "type", "clear_text", "press_enter", "scroll", "drag",
    "long_press", "home", "back", "app_switch", "launch_app",
]


class AndroidAction(BaseAction):
    """One device action accepted by the Android executor."""

    action_type: AndroidActionType  # type: ignore[assignment]
    app: str | None = Field(
        default=None,
        description="Exact semantic application name for launch_app.",
    )

    @model_validator(mode="after")
    def _validate_action_arguments(self) -> "AndroidAction":
        """Validate Android-only argument requirements at the adapter boundary.

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
        if self.action_type == "long_press" and (self.x is None or self.y is None):
            raise ValueError("Android long_press 动作必须填写 x/y")
        if self.action_type == "launch_app" and not str(self.app or "").strip():
            raise ValueError("Android launch_app 动作必须填写 app")
        return self


class AndroidActionDecision(BaseActionDecision):
    action: SerializeAsAny[AndroidAction]  # type: ignore[assignment]
