"""Fixed action execution layer for policy experiments."""

from __future__ import annotations

import subprocess
import time

from policy_expr.utils import clear_text_field, paste_text, press_enter
from policy_expr.recon.yolo_calibrator import YoloCalibrator
from policy_expr.executor_constants import (
    SCROLL_DELTA,
    SCROLL_INTERVAL,
    SCROLL_TICKS,
    WIN_H,
    WIN_H_TAP_MAX,
    WIN_W,
)
from policy_expr.gesture import action_with_drag, action_with_wheel, drag_gesture, wheel_gesture
from Quartz import (
    CGEventCreateMouseEvent,
    CGEventCreateScrollWheelEvent,
    CGEventPost,
    CGEventSetLocation,
    CGWindowListCopyWindowInfo,
    kCGScrollEventUnitPixel,
    kCGHIDEventTap,
    kCGEventMouseMoved,
    kCGEventLeftMouseDown,
    kCGEventLeftMouseUp,
    kCGEventLeftMouseDragged,
    kCGMouseButtonLeft,
    kCGWindowListOptionAll,
    kCGNullWindowID,
)

from policy_expr.perception import LivePhoneSession, try_resume_mac
from policy_expr.schemas import Action, ActionDecision

# App Switcher / kill-app 相关常量（通过像素分析得出）
_TITLE_BAR_H  = 38   # iPhone Mirroring 透明标题栏高度（逻辑 px）
_BTN_Y_OFFSET = 19   # App Switcher 按钮距窗口顶部中心
_BTN_RIGHT    = 22   # App Switcher 按钮距窗口右边缘


class ActionExecutor:
    """Execute normalized policy actions via mirroir-mcp."""

    def __init__(self, phone: LivePhoneSession, calibrator: YoloCalibrator | None = None):
        self.phone = phone
        self.calibrator = calibrator

    def _snap(self, ax: float, ay: float, png_bytes: bytes | None = None, hint: str = "", action: Action | None = None) -> tuple[float, float]:
        """Snap normalized (0-1000) coordinates.

        YOLO and OCR both run when hint is available. OCR wins when it finds a
        specific text match (≥4 chars) — this catches text elements (search bars,
        labels) where YOLO may snap to a nearby icon instead of the actual target.
        """
        yolo_pos = None
        if self.calibrator:
            snapped = self.calibrator.nearest(ax, ay)
            if snapped:
                yolo_pos = snapped

        ocr_pos: tuple[float, float] | None = None
        ocr_len = 0
        if png_bytes and hint:
            ocr = self._ocr_snap(ax, ay, png_bytes, hint)
            if ocr:
                ocr_pos, ocr_len = ocr

        # OCR wins when the matched text is specific enough (≥4 chars) to be
        # trusted over a YOLO icon snap that may have landed on a wrong element.
        if ocr_pos and ocr_len >= 4:
            if yolo_pos:
                print(f"OCR 覆盖 YOLO: ({ax:.0f}, {ay:.0f}) → ({ocr_pos[0]:.0f}, {ocr_pos[1]:.0f}) [匹配={ocr_len}字]")
            else:
                print(f"OCR 吸附: ({ax:.0f}, {ay:.0f}) → ({ocr_pos[0]:.0f}, {ocr_pos[1]:.0f})")
            if action is not None:
                action.snap = {"method": "ocr", "original": [ax, ay], "snapped": list(ocr_pos), "ocr_len": ocr_len}
            return ocr_pos

        if yolo_pos:
            print(f"YOLO 吸附: ({ax:.0f}, {ay:.0f}) → ({yolo_pos[0]:.0f}, {yolo_pos[1]:.0f})")
            if action is not None:
                action.snap = {"method": "yolo", "original": [ax, ay], "snapped": list(yolo_pos)}
            return yolo_pos

        if ocr_pos:
            print(f"OCR 吸附: ({ax:.0f}, {ay:.0f}) → ({ocr_pos[0]:.0f}, {ocr_pos[1]:.0f})")
            if action is not None:
                action.snap = {"method": "ocr", "original": [ax, ay], "snapped": list(ocr_pos), "ocr_len": ocr_len}
            return ocr_pos

        return ax, ay

    def _ocr_snap(self, ax: float, ay: float, png_bytes: bytes, hint: str) -> tuple[tuple[float, float], int] | None:
        """Find OCR text matching hint within 150 normalized units.

        Picks the candidate with the longest matched text first (most specific),
        breaking ties by distance. Returns ((nx, ny), match_len) or None.
        """
        try:
            from policy_expr.utils import ocr_from_bytes
            results, _ = ocr_from_bytes(png_bytes)
        except Exception:
            return None

        candidates: list[tuple[float, int, tuple[float, float]]] = []
        for r in results:
            if len(r.text) < 2 or r.text not in hint:
                continue
            tx, ty = r.tap_coords(WIN_W, WIN_H)
            nx, ny = tx / WIN_W * 1000, ty / WIN_H * 1000
            dist = ((nx - ax) ** 2 + (ny - ay) ** 2) ** 0.5
            if dist <= 150:
                candidates.append((dist, len(r.text), (nx, ny)))

        if not candidates:
            return None

        # Longer match = more specific = preferred; break ties by distance.
        candidates.sort(key=lambda c: (-c[1], c[0]))
        _, best_len, best_pos = candidates[0]
        return best_pos, best_len

    def execute(self, decision: ActionDecision, app_name: str = "", png_bytes: bytes | None = None) -> bool:
        action = decision.action
        print(f"\n动作: [{action.action_type}] {action.description}")

        hint = action.description or ""
        if action.action_type in ("tap", "click") and action.x is not None and action.y is not None:
            ax, ay = self._snap(action.x, action.y, png_bytes=png_bytes, hint=hint, action=action)
            lx, ly = logical_xy(ax, ay)
            if not self._tap(lx, ly, decision, app_name):
                return False

        elif action.action_type == "type" and action.text:
            if action.x is not None and action.y is not None:
                ax, ay = self._snap(action.x, action.y, png_bytes=png_bytes, hint=hint, action=action)
                lx, ly = logical_xy(ax, ay)
                if not self._tap(lx, ly, decision, app_name):
                    return False
                time.sleep(0.5)
            else:
                print("未提供输入坐标，默认当前输入框已聚焦，直接输入文字")
            # Clear first so existing text is replaced, not appended.
            clear_text_field()
            time.sleep(0.2)
            print(f"输入文字: {action.text!r}")
            paste_text(action.text)
            print("结果: 已通过剪贴板粘贴输入")

        elif action.action_type == "clear_text":
            print("清空当前输入框")
            clear_text_field()

        elif action.action_type == "press_enter":
            print("按回车确认输入")
            press_enter()

        elif action.action_type == "scroll" and action.direction:
            if action.method == "drag":
                self._drag(action_with_drag(action, drag_gesture(action)))
            else:
                gesture = wheel_gesture(action)
                self._scroll(action_with_wheel(action, gesture), ticks=gesture.ticks, delta_px=gesture.delta_px)

        elif action.action_type == "drag":
            self._drag(action_with_drag(action, drag_gesture(action)))

        elif action.action_type == "home":
            print("执行返回主屏")
            result = self._client().press_home()
            if "Failed to press Home" in result:
                print(f"结果: {result}，改为点击底部 Home 指示条")
                result = self._client().tap(WIN_W / 2, WIN_H - 16)
            print(f"结果: {result}")

        elif action.action_type == "kill_frontmost_app":
            print("执行：打开 App Switcher 并 kill 最前台 App")
            return self._kill_frontmost_app()

        elif action.action_type == "stop":
            print("停止操作（当前状态已满足目标，无需执行）")
            return True

        else:
            print(f"跳过执行：action_type={action.action_type!r}，需手动处理")
            return False

        return True

    def _tap(self, lx: float, ly: float, decision: ActionDecision, app_name: str = "") -> bool:
        print(f"执行点击: ({lx:.0f}, {ly:.0f})")
        result = self._client().tap(lx, ly)
        print(f"结果: {result}")
        if "paused" in result.lower():
            print("Mac 弹窗阻断，尝试恢复...")
            if try_resume_mac():
                time.sleep(0.5)
                result = self._client().tap(lx, ly)
                print(f"恢复后重试: {result}")
        if "interrupted" in result.lower() or "failed" in result.lower():
            print("点击失败：落点在窗口外或操作被中断，跳过")
            return False
        return True

    def _kill_frontmost_app(self) -> bool:
        """打开 App Switcher，上滑 kill 最前台 app，点底部空白区回主屏。"""
        origin = _find_iphone_window()
        if origin is None:
            print("  iPhone Mirroring 窗口未找到，跳过")
            return False
        wx, wy = origin

        # 激活窗口（标题栏透明，不激活直接点会穿透到桌面）
        subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to tell process "iPhone Mirroring" to set frontmost to true'],
            capture_output=True,
        )
        time.sleep(0.4)

        # A. 点右上角 App Switcher 按钮
        btn_x = wx + WIN_W - _BTN_RIGHT
        btn_y = wy + _BTN_Y_OFFSET
        print(f"  A. 点 App Switcher 按钮 ({btn_x:.0f}, {btn_y:.0f})")
        _quartz_hover(btn_x, btn_y)
        time.sleep(0.2)
        _quartz_click(btn_x, btn_y)
        time.sleep(2.0)

        # B. 上滑 kill 最前台卡片（卡片在右侧 80% 处）
        content_top = wy + _TITLE_BAR_H
        swipe_x = wx + WIN_W * 0.80
        print(f"  B. 上滑 kill 卡片: x={swipe_x:.0f}, y {content_top+350:.0f}→{content_top+80:.0f}")
        _quartz_swipe(swipe_x, content_top + 350, swipe_x, content_top + 80, duration_ms=350)
        time.sleep(1.5)

        # C. 点底部导航空白区退出 App Switcher
        nav_x = wx + WIN_W / 2
        nav_y = wy + WIN_H - 60
        print(f"  C. 点底部空白区退出 Switcher ({nav_x:.0f}, {nav_y:.0f})")
        _quartz_click(nav_x, nav_y)
        time.sleep(1.0)
        return True

    def execute_scroll(self, action: Action, *, ticks: int = SCROLL_TICKS, delta_px: int = SCROLL_DELTA) -> None:
        self._scroll(action, ticks=ticks, delta_px=delta_px)

    def execute_drag(self, action: Action) -> None:
        self._drag(action)

    def _scroll(self, action: Action, *, ticks: int = SCROLL_TICKS, delta_px: int = SCROLL_DELTA) -> None:
        direction = (action.direction or "").strip().lower()
        delta = _scroll_delta(direction, delta_px)

        # Find iPhone Mirroring window on screen
        origin = _find_iphone_window()
        if origin is None:
            print("  iPhone Mirroring 窗口未找到，无法发送滚轮事件")
            return
        wx, wy = origin

        # Activate window (MCP connections can steal focus)
        subprocess.run(["osascript", "-e", 'tell application "iPhone Mirroring" to activate'],
                       capture_output=True)
        time.sleep(0.3)

        # Normalized (0-1000) → screen coordinates
        ax = action.x if action.x is not None else 500
        ay = action.y if action.y is not None else 500
        sx = wx + ax / 1000 * WIN_W
        sy = wy + ay / 1000 * WIN_H

        print(f"  鼠标移至 ({sx:.0f}, {sy:.0f})，滚轮 {direction} × {ticks}")

        # Move cursor to target position
        move = CGEventCreateMouseEvent(None, kCGEventMouseMoved, (sx, sy), kCGMouseButtonLeft)
        CGEventPost(kCGHIDEventTap, move)
        time.sleep(0.1)

        # Send scroll wheel ticks
        for i in range(ticks):
            ev = CGEventCreateScrollWheelEvent(None, kCGScrollEventUnitPixel, 1, delta)
            CGEventSetLocation(ev, (sx, sy))
            CGEventPost(kCGHIDEventTap, ev)
            if i < ticks - 1:
                time.sleep(SCROLL_INTERVAL)

    def _drag(self, action: Action) -> None:
        if action.x is None or action.y is None or action.to_x is None or action.to_y is None:
            print("拖动失败：缺少 x/y/to_x/to_y")
            return

        from_x, from_y = logical_xy(action.x, action.y)
        to_x, to_y = logical_xy(action.to_x, action.to_y)
        duration_ms = action.duration_ms or 1200

        print(
            "执行拖动: "
            f"({from_x:.0f},{from_y:.0f}) → ({to_x:.0f},{to_y:.0f}), "
            f"{duration_ms}ms"
        )
        _activate_iphone_mirroring()
        _hover_logical(from_x, from_y)
        result = self._client().drag(from_x, from_y, to_x, to_y, duration_ms)
        print(f"结果: {result}")

    def _client(self):
        if not self.phone.client:
            raise RuntimeError("MCP 尚未连接")
        return self.phone.client


def logical_xy(ax: float, ay: float) -> tuple[float, float]:
    """Convert normalized coordinates to iPhone Mirroring logical pixels."""
    x = ax / 1000 * WIN_W
    y = ay / 1000 * WIN_H
    return max(0, min(x, WIN_W - 1)), max(0, min(y, WIN_H_TAP_MAX))


# Valid tap region on the phone screen (logical 0-1000 space).
# Derived from mcp_frame_mask.png: corners, notch (top ~50px), and edges are frame/desktop.
_TAP_X_MIN, _TAP_X_MAX = 30, 970
_TAP_Y_MIN, _TAP_Y_MAX = 80, 970


def is_valid_tap(ax: float, ay: float) -> bool:
    """Check if normalized (0-1000) coordinates fall on the phone screen, not the frame/desktop."""
    return _TAP_X_MIN <= ax <= _TAP_X_MAX and _TAP_Y_MIN <= ay <= _TAP_Y_MAX


def _find_iphone_window() -> tuple[float, float] | None:
    """Return (x, y) screen origin of the 318x701 iPhone Mirroring window."""
    windows = CGWindowListCopyWindowInfo(kCGWindowListOptionAll, kCGNullWindowID)
    for w in windows:
        owner = w.get("kCGWindowOwnerName", "")
        if "iPhone" not in owner:
            continue
        bounds = w.get("kCGWindowBounds", {})
        ww, wh = bounds.get("Width", 0), bounds.get("Height", 0)
        if int(ww) == 318 and int(wh) == 701:
            return (bounds["X"], bounds["Y"])
    return None


def _quartz_hover(x: float, y: float) -> None:
    ev = CGEventCreateMouseEvent(None, kCGEventMouseMoved, (x, y), kCGMouseButtonLeft)
    CGEventPost(kCGHIDEventTap, ev)


def _quartz_click(x: float, y: float) -> None:
    pt = (x, y)
    for ev_type in (kCGEventMouseMoved, kCGEventLeftMouseDown, kCGEventLeftMouseUp):
        ev = CGEventCreateMouseEvent(None, ev_type, pt, kCGMouseButtonLeft)
        CGEventPost(kCGHIDEventTap, ev)
        time.sleep(0.05)


def _activate_iphone_mirroring() -> None:
    subprocess.run(
        ["osascript", "-e", 'tell application "iPhone Mirroring" to activate'],
        capture_output=True,
    )
    time.sleep(0.4)


def _hover_logical(lx: float, ly: float) -> None:
    origin = _find_iphone_window()
    if origin is None:
        print("  iPhone Mirroring 窗口未找到，跳过拖动前 hover")
        return
    wx, wy = origin
    sx = wx + lx
    sy = wy + ly
    _quartz_hover(sx, sy)
    time.sleep(0.2)


def _quartz_swipe(from_x: float, from_y: float, to_x: float, to_y: float,
                  duration_ms: int = 350) -> None:
    steps = max(10, duration_ms // 16)
    dx = (to_x - from_x) / steps
    dy = (to_y - from_y) / steps

    ev = CGEventCreateMouseEvent(None, kCGEventLeftMouseDown, (from_x, from_y), kCGMouseButtonLeft)
    CGEventPost(kCGHIDEventTap, ev)
    time.sleep(0.02)

    for i in range(1, steps + 1):
        pt = (from_x + dx * i, from_y + dy * i)
        ev = CGEventCreateMouseEvent(None, kCGEventLeftMouseDragged, pt, kCGMouseButtonLeft)
        CGEventPost(kCGHIDEventTap, ev)
        time.sleep(duration_ms / 1000 / steps)

    ev = CGEventCreateMouseEvent(None, kCGEventLeftMouseUp, (to_x, to_y), kCGMouseButtonLeft)
    CGEventPost(kCGHIDEventTap, ev)


def _scroll_delta(direction: str, magnitude: int = SCROLL_DELTA) -> int:
    """Map scroll direction to CGEvent scroll wheel delta (pixels per tick).

    Positive = scroll up (content moves down, view earlier content).
    Negative = scroll down (content moves up, view later content).
    """
    if direction in ("up", "向上", "upward"):
        return magnitude
    if direction in ("down", "向下", "downward"):
        return -magnitude
    if direction in ("left", "向左", "leftward"):
        return -magnitude
    if direction in ("right", "向右", "rightward"):
        return magnitude
    raise ValueError(f"scroll direction must be up/down/left/right, got: {direction!r}")
