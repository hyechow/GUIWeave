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
    r"\b([A-Za-z][A-Za-z0-9_.:-]{2,})(?:\s*\([^)]*\))?['\"]?"
    r"\s*[^行]{0,8}?行(?:右侧|右边)?(?:的)?\s*['\"]?"
    r"(Add|Remove)['\"]?\s*按钮",
    re.I,
)
_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def _row_button_coords(
    xml_text: str, target: str, button_text: str, win_w: int, win_h: int,
) -> tuple[float, float] | None:
    """Return the 0-1000 normalized center of the Add/Remove button on ``target``'s
    row, parsed from a UIAutomator dump. Pure (no device I/O) so it is unit-testable;
    the executor wraps this with the dump + snap decision. ``button_text``/``target``
    are already lower-cased by the caller."""
    try:
        root = ET.fromstring(xml_text)
    except Exception:  # noqa: BLE001
        return None
    nodes: list[tuple[str, int, int, int, int]] = []
    for node in root.iter("node"):
        bm = _BOUNDS_RE.search(node.get("bounds") or "")
        if not bm:
            continue
        text = (node.get("text") or node.get("content-desc") or "").strip()
        if not text:
            continue
        x1, y1, x2, y2 = (int(bm.group(i)) for i in (1, 2, 3, 4))
        if x2 <= 0 or y2 <= 0 or x1 >= win_w or y1 >= win_h:
            continue
        nodes.append((unescape(text), x1, y1, x2, y2))

    target_centers = [
        (y1 + y2) / 2
        for _text, _x1, y1, _x2, y2 in nodes
        if _text.strip().lower() in (target, f"@{target}")
    ]
    if not target_centers:
        return None
    row_y = sum(target_centers) / len(target_centers)

    candidates: list[tuple[float, int, int, int, int]] = []
    for text, x1, y1, x2, y2 in nodes:
        if text.strip().lower() != button_text:
            continue
        if (x1 + x2) / 2 < win_w * 0.55:  # right-side buttons only
            continue
        cy = (y1 + y2) / 2
        if abs(cy - row_y) <= 130:
            candidates.append((abs(cy - row_y), x1, y1, x2, y2))
    if not candidates:
        return None
    _dist, x1, y1, x2, y2 = min(candidates, key=lambda item: item[0])
    return ((x1 + x2) / 2) / win_w * 1000, ((y1 + y2) / 2) / win_h * 1000


_MENU_ITEM_RE = re.compile(
    r"(?:下拉菜单|下拉|菜单|列表|popup|dropdown)"
    r"[\s\S]{0,20}?"
    r"(?:"
    r"['\"]\s*([A-Za-z][^'\"\n]{1,30})\s*['\"]"  # 引号目标: 'Lists'
    r"|"
    r"\b([A-Za-z][A-Za-z0-9_. -]{1,30}?)\s+(?:选项|项|标题栏|按钮|条目)"  # 无引号 + 后缀: Lists 选项
    r")",
    re.I,
)


def _menu_item_coords(
    xml_text: str, target: str, win_w: int, win_h: int,
) -> tuple[float, float] | None:
    """Return the 0-1000 normalized center of the unique a11y node whose text or
    content-desc equals ``target`` (case-insensitive). Pure (no device I/O) so it is
    unit-testable. Returns None on zero or multiple matches (ambiguous → don't snap,
    let vision decide). Used for dropdown/menu/list item taps (e.g. Mastodon Home
    dropdown -> Lists) where vision jitters between adjacent rows."""
    try:
        root = ET.fromstring(xml_text)
    except Exception:  # noqa: BLE001
        return None
    target_lc = (target or "").strip().lower()
    if not target_lc:
        return None
    matches: list[tuple[int, int, int, int]] = []
    for node in root.iter("node"):
        bm = _BOUNDS_RE.search(node.get("bounds") or "")
        if not bm:
            continue
        text = (node.get("text") or node.get("content-desc") or "").strip()
        if text.lower() != target_lc:
            continue
        x1, y1, x2, y2 = (int(bm.group(i)) for i in (1, 2, 3, 4))
        if x2 <= 0 or y2 <= 0 or x1 >= win_w or y1 >= win_h:
            continue
        matches.append((x1, y1, x2, y2))
    if len(matches) != 1:
        return None
    x1, y1, x2, y2 = matches[0]
    return ((x1 + x2) / 2) / win_w * 1000, ((y1 + y2) / 2) / win_h * 1000


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
            # a11y snaps match on the full targeting text. action.description is often
            # a generic "执行tap操作", so prefer decision.instruction (the supervisor's
            # full instruction) which carries the actual target.
            desc = f"{decision.instruction or ''} {action.description or ''}"
            corrected = self._resolve_row_button_tap(desc)
            if corrected is None:
                corrected = self._resolve_menu_item_tap(desc)
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

        coords = _row_button_coords(xml_text, target, button_text, client.win_w, client.win_h)
        if coords is not None:
            nx, ny = coords
            print(
                f"  [AndroidSnap] {target} 行 {button_text.title()} "
                f"按钮坐标校正: ({nx:.0f},{ny:.0f})"
            )
        return coords

    def _resolve_menu_item_tap(self, description: str) -> tuple[float, float] | None:
        """Snap '点击下拉菜单/列表里的 X 选项/项' taps to the matching a11y node.

        Mastodon's Home dropdown (and similar list/popup menus) renders each item as a
        TextView with stable bounds, but vision's coordinate estimate jitters between
        adjacent rows and occasionally taps the wrong item (e.g. Live feed instead of
        Lists). UIAutomator reads item bounds directly, so snap when the instruction
        explicitly targets a menu/list item."""
        match = _MENU_ITEM_RE.search(description or "")
        if not match:
            return None
        target = (match.group(1) or match.group(2) or "").strip()
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

        coords = _menu_item_coords(xml_text, target, client.win_w, client.win_h)
        if coords is not None:
            nx, ny = coords
            print(f"  [AndroidSnap] 菜单项 {target} 坐标校正: ({nx:.0f},{ny:.0f})")
        return coords

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
