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


# Android scroll units per ScrollAmount label. WIDER range than the neutral 3/5/9 so
# `small` is a fine wheel-picker nudge (~1 row) while `large` still flings a list:
# with SCROLL_PX_PER_AMOUNT≈140 -> small≈140px (≈1 picker row), medium≈560px (≈¼
# screen), large≈1120px (≈½ screen).
_ANDROID_AMOUNT_UNITS = {"small": 1, "medium": 4, "large": 8}


class AndroidExecutor(VisionExecutor):
    """Execute normalized policy actions against the phone via AndroidDevice."""

    def _client(self):
        client = getattr(self.session, "client", None)
        if client is None:
            raise RuntimeError("Android 设备尚未连接")
        return client

    def _amount_units(self, amount: str) -> int:
        return _ANDROID_AMOUNT_UNITS.get(amount, 4)

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
