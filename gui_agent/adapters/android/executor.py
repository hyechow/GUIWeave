"""Android action executor: dispatch one ActionDecision onto the device.

Reuses the shared ``gui_agent.core.runtime.executor.VisionExecutor`` (the 7 shared actions +
denorm + _tap; default _clear_before_type = clear_text suits android). Only android's
nav keys live here, in ``_dispatch_extra``: home / back / app_switch.

Coordinates: adb screencap is device pixels and ``input`` consumes the same space, so
the inherited ``_denorm`` (normalized 0-1000 -> ``viewport_size`` px) goes straight to
``input tap`` — no window/retina mapping.
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from html import unescape
from typing import Optional

from gui_agent.adapters.android.actions import AndroidAction
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
_ROW_BUTTON_RE = re.compile(
    r"\b([A-Za-z][A-Za-z0-9_.:-]{2,})(?:\s*\([^)]*\))?\s*行右侧(?:的)?\s*(Add|Remove)\s*按钮",
    re.I,
)
_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


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
        if action.action_type == "tap":
            corrected = self._resolve_row_button_tap(action.description or "")
            if corrected is not None:
                action.x, action.y = corrected
        return super().execute(decision, app_name=app_name, png_bytes=png_bytes, is_home_screen=is_home_screen)

    def _resolve_row_button_tap(self, description: str) -> tuple[float, float] | None:
        """Snap '<user> 行右侧 Add/Remove 按钮' taps to the matching a11y node.

        Vision sometimes confuses adjacent Mastodon member rows because the Add buttons
        are vertically dense. UIAutomator gives stable visible bounds for the username
        labels and right-side Add/Remove buttons, so use it only for this explicit row
        button instruction shape and leave all other taps untouched.
        """

        match = _ROW_BUTTON_RE.search(description)
        if not match:
            return None
        target = match.group(1).lower()
        button_text = match.group(2).lower()
        client = self._client()
        dev = getattr(client, "_dev", None)
        if dev is None:
            return None

        remote = "/sdcard/_gui_agent_exec_ui.xml"
        xml_text = ""
        for attempt in range(2):
            try:
                dev.shell(f"uiautomator dump {remote}")
                xml_text = dev.shell(f"cat {remote}")
            except Exception:  # noqa: BLE001
                xml_text = ""
            if "<node" in xml_text:
                break
            if attempt == 0:
                time.sleep(0.2)
        if "<node" not in xml_text:
            return None

        try:
            root = ET.fromstring(xml_text)
        except Exception:  # noqa: BLE001
            return None

        nodes: list[tuple[str, int, int, int, int]] = []
        for node in root.iter("node"):
            bounds = node.get("bounds") or ""
            bm = _BOUNDS_RE.search(bounds)
            if not bm:
                continue
            text = (node.get("text") or node.get("content-desc") or "").strip()
            if not text:
                continue
            x1, y1, x2, y2 = (int(bm.group(i)) for i in (1, 2, 3, 4))
            if x2 <= 0 or y2 <= 0 or x1 >= client.win_w or y1 >= client.win_h:
                continue
            nodes.append((unescape(text), x1, y1, x2, y2))

        target_centers: list[float] = []
        for text, _x1, y1, _x2, y2 in nodes:
            norm = text.strip().lower()
            if norm == target or norm == f"@{target}":
                target_centers.append((y1 + y2) / 2)
        if not target_centers:
            return None
        row_y = sum(target_centers) / len(target_centers)

        candidates: list[tuple[float, int, int, int, int]] = []
        for text, x1, y1, x2, y2 in nodes:
            if text.strip().lower() != button_text:
                continue
            if (x1 + x2) / 2 < client.win_w * 0.55:
                continue
            cy = (y1 + y2) / 2
            dist = abs(cy - row_y)
            if dist <= 130:
                candidates.append((dist, x1, y1, x2, y2))
        if not candidates:
            return None

        _dist, x1, y1, x2, y2 = min(candidates, key=lambda item: item[0])
        nx = ((x1 + x2) / 2) / client.win_w * 1000
        ny = ((y1 + y2) / 2) / client.win_h * 1000
        print(
            f"  [AndroidSnap] {target} 行右侧 {button_text.title()} "
            f"坐标校正: ({nx:.0f},{ny:.0f})"
        )
        return nx, ny

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
