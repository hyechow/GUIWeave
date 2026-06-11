"""Android action executor: dispatch one ActionDecision onto the device.

Reuses the shared ``gui_agent.core.executor.VisionExecutor`` (the 7 shared actions +
denorm + _tap; default _clear_before_type = clear_text suits android). Only android's
nav keys live here, in ``_dispatch_extra``: home / back / app_switch.

Coordinates: adb screencap is device pixels and ``input`` consumes the same space, so
the inherited ``_denorm`` (normalized 0-1000 -> ``viewport_size`` px) goes straight to
``input tap`` — no window/retina mapping.
"""

from __future__ import annotations

from typing import Optional

from gui_agent.adapters.android.actions import AndroidAction
from gui_agent.core.executor import VisionExecutor


# Android scroll units per ScrollAmount label for ordinary lists. Picker wheels use
# column-specific maps below; the old shared medium=4 jumped ~8 hour rows and caused
# 09<->01 oscillation in the alarm picker.
_ANDROID_AMOUNT_UNITS = {"small": 1, "medium": 4, "large": 8}
_ANDROID_PICKER_AMOUNT_UNITS = {
    "default": {"small": 1, "medium": 2, "large": 3},
    "hour": {"small": 1, "medium": 2, "large": 2},
    "period": {"small": 1, "medium": 1, "large": 1},
    "ampm": {"small": 1, "medium": 1, "large": 1},
    "minute": {"small": 1, "medium": 2, "large": 3},
}


class AndroidExecutor(VisionExecutor):
    """Execute normalized policy actions against the phone via AndroidDevice."""

    def _client(self):
        client = getattr(self.session, "client", None)
        if client is None:
            raise RuntimeError("Android 设备尚未连接")
        return client

    def _amount_units(self, amount: str) -> int:
        return _ANDROID_AMOUNT_UNITS.get(amount, 4)

    def _amount_units_for_action(self, action: AndroidAction) -> int:
        snap = action.snap or {}
        column = snap.get("picker_column")
        if action.action_type == "scroll" and column:
            amount_map = _ANDROID_PICKER_AMOUNT_UNITS.get(column, _ANDROID_PICKER_AMOUNT_UNITS["default"])
            return amount_map.get(action.amount, 1)
        return self._amount_units(action.amount)

    def _execute_scroll_action(self, action: AndroidAction) -> None:
        client = self._client()
        ax = action.x if action.x is not None else 500
        ay = action.y if action.y is not None else 500
        px, py = self._denorm(ax, ay)
        amount = self._amount_units_for_action(action)
        direction = action.direction or "down"
        print(f"  scroll {direction} amount={amount} @({px:.0f},{py:.0f})")
        print(f"  结果: {client.scroll(direction, amount, px, py)}")

    def execute_scroll(self, action, *, ticks: int = 0, delta_px: int = 0) -> None:
        self._execute_scroll_action(action)

    def execute(self, decision, app_name: str = "", png_bytes=None, is_home_screen: bool = False) -> bool:
        action = decision.action
        if action.action_type == "scroll" and action.direction:
            print(f"\n动作: [{action.action_type}] {action.description}")
            self._execute_scroll_action(action)
            return True
        return super().execute(decision, app_name=app_name, png_bytes=png_bytes, is_home_screen=is_home_screen)

    def _dispatch_extra(self, action: AndroidAction, client) -> Optional[bool]:
        at = action.action_type
        if at == "home":
            print("回到主屏幕")
            print(f"  结果: {client.press_home()}")
            return True
        if at == "back":
            print("返回上一级")
            print(f"  结果: {client.back()}")
            return True
        if at == "app_switch":
            print("打开 App 切换器（多任务）")
            print(f"  结果: {client.app_switch()}")
            return True
        return None
