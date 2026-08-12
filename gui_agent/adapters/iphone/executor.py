"""iPhone action execution through the shared vision executor."""

from __future__ import annotations

from typing import Optional

from gui_agent.adapters.iphone.actions import IPhoneAction
from gui_agent.core.runtime.executor import VisionExecutor


class IPhoneExecutor(VisionExecutor):
    """Execute the unified action contract against iPhone Mirroring."""

    def execute(self, decision, **kwargs: object) -> bool:
        action = decision.action
        if action.action_type in {"tap", "type", "scroll", "drag"}:
            if action.y is not None:
                action.y = max(80.0, min(float(action.y), 970.0))
            if action.to_y is not None:
                action.to_y = max(80.0, min(float(action.to_y), 970.0))
        return super().execute(decision, **kwargs)

    def _dispatch_extra(self, action: IPhoneAction, client) -> Optional[bool]:
        if action.action_type == "home":
            result = client.press_home()
            print(f"  结果: {result}")
            return self._result_succeeded(result, "回到主屏幕")
        if action.action_type == "app_switch":
            result = client.app_switch()
            print(f"  结果: {result}")
            return self._result_succeeded(result, "打开 App 切换器")
        return None


__all__ = ["IPhoneExecutor"]
