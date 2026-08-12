"""Android action executor: dispatch one ActionDecision onto the device.

Reuses the shared ``gui_agent.core.runtime.executor.VisionExecutor`` (the shared actions +
denorm + _tap; default _clear_before_type = clear_text suits android). Android-specific
long-press, semantic app launch, and navigation keys live in ``_dispatch_extra``.

Coordinates: adb screencap is device pixels and ``input`` consumes the same space, so
the inherited ``_denorm`` (normalized 0-1000 -> ``viewport_size`` px) goes straight to
``input tap`` — no window/retina mapping.
"""

from __future__ import annotations

import re
from typing import Optional

from gui_agent.adapters.android.actions import AndroidAction
from gui_agent.adapters.android.accessibility import (
    form_controls_from_semantic_tree,
    semantic_tree_from_uiautomator,
)
from gui_agent.adapters.android.control_grounding import (
    ground_action_to_android_control,
)
from gui_agent.core.runtime.executor import VisionExecutor


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
_SLIDER_CONTROL = re.compile(
    r"(?:\bslider\b|\bseek\s*bar\b|\bseekbar\b|滑块)",
    re.IGNORECASE,
)
_SLIDER_EDGE_THRESHOLD = 900.0


class AndroidExecutor(VisionExecutor):
    """Execute normalized policy actions against the phone via AndroidDevice."""

    # Focusing a field can open the keyboard and reflow the Android viewport.
    # Typing or focusing can reflow the viewport, invalidating a spatial suffix.
    tap_type_suffix_safe = False
    type_suffix_safe = False

    def refresh_controls(self) -> list[dict] | None:
        """Refresh UIAutomator controls for safe in-batch coordinate rebinding."""
        client = self._client()
        return form_controls_from_semantic_tree(semantic_tree_from_uiautomator(
            client.dump_ui_hierarchy(),
            viewport_size=client.viewport_size,
        ))

    def ground_coordinates(self, decision, controls):
        """Use current UIAutomator identity to correct a bounded visual miss."""
        return ground_action_to_android_control(decision, controls)

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

    def _execute_scroll_action(self, action: AndroidAction) -> bool:
        client = self._client()
        ax = action.x if action.x is not None else 500
        ay = action.y if action.y is not None else 500
        px, py = self._denorm(ax, ay)
        amount = self._amount_units_for_action(action)
        direction = action.direction or "down"
        print(f"  scroll {direction} amount={amount} @({px:.0f},{py:.0f})")
        result = client.scroll(direction, amount, px, py)
        print(f"  结果: {result}")
        return self._result_succeeded(result, "滚动")

    def execute_scroll(self, action, *, ticks: int = 0, delta_px: int = 0) -> bool:
        return self._execute_scroll_action(action)

    def execute(
        self,
        decision,
        app_name: str = "",
        png_bytes=None,
        is_home_screen: bool = False,
        target_control: str = "",
    ) -> bool:
        action = decision.action
        if (
            action.action_type == "drag"
            and _SLIDER_CONTROL.search(target_control)
            and action.x is not None
            and action.y is not None
            and action.to_x is not None
            and action.to_y is not None
            and abs(action.to_x - action.x) >= abs(action.to_y - action.y)
        ):
            if action.to_x >= _SLIDER_EDGE_THRESHOLD:
                action.to_x = 1000.0
            elif action.to_x <= 1000.0 - _SLIDER_EDGE_THRESHOLD:
                action.to_x = 0.0
        if action.action_type == "scroll" and action.direction:
            print(f"\n动作: [{action.action_type}] {action.description}")
            return self._execute_scroll_action(action)
        return super().execute(
            decision,
            app_name=app_name,
            png_bytes=png_bytes,
            is_home_screen=is_home_screen,
            target_control=target_control,
        )

    def _dispatch_extra(self, action: AndroidAction, client) -> Optional[bool]:
        at = action.action_type
        if at == "long_press":
            px, py = self._denorm(action.x, action.y)
            duration_ms = action.duration_ms or 600
            print(f"长按: ({px:.0f}, {py:.0f}), {duration_ms}ms")
            result = client.long_press(px, py, duration_ms)
            print(f"  结果: {result}")
            return self._result_succeeded(result, "长按")
        if at == "home":
            print("回到主屏幕")
            result = client.press_home()
            print(f"  结果: {result}")
            return self._result_succeeded(result, "回到主屏幕")
        if at == "back":
            print("返回上一级")
            result = client.back()
            print(f"  结果: {result}")
            return self._result_succeeded(result, "返回")
        if at == "app_switch":
            print("打开 App 切换器（多任务）")
            result = client.app_switch()
            print(f"  结果: {result}")
            return self._result_succeeded(result, "打开 App 切换器")
        if at == "launch_app":
            print(f"启动应用: {action.app}")
            result = client.launch_app(action.app)
            print(f"  结果: {result}")
            return self._result_succeeded(result, "启动应用")
        return None
