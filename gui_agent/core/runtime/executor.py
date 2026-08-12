"""VisionExecutor — shared dispatch for vision-only platforms (browser + android).

Both drive a ``Device`` (tap/type/scroll/drag/clear_text/press_enter) with normalized
0-1000 coords denormalized against the device's ``viewport_size``. Their dispatch of
the 7 shared actions was ~identical; this base factors it out. A platform subclass
overrides only:
  - ``_dispatch_extra`` — its platform-specific actions (browser: navigate/back/tabs;
    android: home/back/app_switch). Returns a bool when handled, None otherwise.
  - ``_clear_before_type`` — how the focused field is cleared before a ``type``
    (default: ``clear_text``; browser overrides for contenteditable safety).

Every device primitive returns a status string.  Dispatch treats ``failed``,
``interrupted`` and ``paused`` uniformly for every action so the runner never
records an unsuccessful device call as executed.

iPhone does NOT use this base — its executor is genuinely different (YOLO/OCR snap,
2x-retina ``logical_xy``, picker gesture computation, daemon/mirroir text paths).

``execute`` matches the iphone ``ActionExecutor.execute`` signature exactly
(``decision, app_name='', png_bytes=None, is_home_screen=False`` — the trailing
iphone-only args are accepted and ignored) so the generic runner drives it the same.
"""

from __future__ import annotations

import time
from typing import Optional

from gui_agent.core.schemas import BaseActionDecision

# Coarse ScrollAmount -> device ``scroll`` amount units (the device maps units->px).
_AMOUNT_UNITS = {"small": 3, "medium": 5, "large": 9}
_REDACTED_INPUT_VALUE = "[session access value redacted]"


def amount_to_units(amount: str) -> int:
    return _AMOUNT_UNITS.get(amount, 5)


class VisionExecutor:
    """Execute normalized policy actions against a vision-only Device."""

    def __init__(self, session):
        # ``session`` holds the connected Device on ``.client``.
        self.session = session

    def _client(self):
        client = getattr(self.session, "client", None)
        if client is None:
            raise RuntimeError("设备尚未连接")
        return client

    def _denorm(self, ax: float, ay: float) -> tuple[float, float]:
        """Normalized 0-1000 -> device pixels, clamped inside the viewport."""
        w, h = self._client().viewport_size
        x = ax / 1000 * w
        y = ay / 1000 * h
        return max(0.0, min(x, w - 1)), max(0.0, min(y, h - 1))

    def prepare_frame(self, png_bytes: bytes) -> None:
        """No-op: vision-only adapters have nothing to precompute (no YOLO/OCR snap).
        The runner submits this to a background thread each turn."""
        return None

    def _amount_units(self, amount: str) -> int:
        """Map a coarse ScrollAmount label to device ``scroll`` units. Default is the
        neutral small/medium/large -> 3/5/9. A platform overrides this to widen the
        range when it needs both fine (wheel-picker, ~1 row) and coarse (list) steps."""
        return amount_to_units(amount)

    def _display_value(self, value: object) -> str:
        """Render executor output without persisting configured access values."""

        rendered = str(value)
        for secret in getattr(self, "sensitive_text_values", ()):
            if secret:
                rendered = rendered.replace(secret, _REDACTED_INPUT_VALUE)
        return rendered

    def execute_scroll(self, action, *, ticks: int = 0, delta_px: int = 0) -> bool:
        """Scroll without execute()'s bool wrapper (runner scroll-cache path).
        ``ticks`` / ``delta_px`` are iphone scroll-probe params and are ignored."""
        client = self._client()
        ax = action.x if action.x is not None else 500
        ay = action.y if action.y is not None else 500
        px, py = self._denorm(ax, ay)
        amount = self._amount_units(action.amount)
        direction = action.direction or "down"
        print(f"  scroll {direction} amount={amount} @({px:.0f},{py:.0f})")
        result = client.scroll(direction, amount, px, py)
        print(f"  结果: {result}")
        return self._result_succeeded(result, "滚动")

    def execute(
        self,
        decision: BaseActionDecision,
        app_name: str = "",
        png_bytes: bytes | None = None,
        is_home_screen: bool = False,
        target_control: str = "",
    ) -> bool:
        # app_name / png_bytes / is_home_screen / target_control are adapter seams.
        action = decision.action
        print(f"\n动作: [{action.action_type}] {action.description}")
        client = self._client()

        if action.action_type in ("tap", "click") and action.x is not None and action.y is not None:
            px, py = self._denorm(action.x, action.y)
            return self._tap(px, py)

        elif action.action_type == "type" and action.text:
            if action.x is not None and action.y is not None:
                px, py = self._denorm(action.x, action.y)
                if not self._tap(px, py):
                    return False
                time.sleep(0.3)
            else:
                print("未提供输入坐标，默认当前输入框已聚焦，直接输入文字")
            if not self._type_intercept(client, action.text):
                if not self._clear_before_type(client, action.text):
                    return False
                result = client.type_text(action.text)
                print(f"  结果: {self._display_value(result)}")
                return self._result_succeeded(result, "输入")
            return True

        elif action.action_type == "clear_text":
            print("清空当前输入框")
            result = client.clear_text()
            print(f"  结果: {result}")
            return self._result_succeeded(result, "清空")

        elif action.action_type == "press_enter":
            print("按回车确认输入")
            result = client.press_enter()
            print(f"  结果: {result}")
            return self._result_succeeded(result, "回车")

        elif action.action_type == "scroll" and action.direction:
            return self.execute_scroll(action)

        elif action.action_type == "drag":
            if action.x is None or action.y is None or action.to_x is None or action.to_y is None:
                print("拖动失败：缺少 x/y/to_x/to_y")
                return False
            fx, fy = self._denorm(action.x, action.y)
            tx, ty = self._denorm(action.to_x, action.to_y)
            duration_ms = action.duration_ms or 1000
            print(f"  drag ({fx:.0f},{fy:.0f})->({tx:.0f},{ty:.0f}), {duration_ms}ms")
            result = client.drag(fx, fy, tx, ty, duration_ms)
            print(f"  结果: {result}")
            return self._result_succeeded(result, "拖动")

        else:
            handled = self._dispatch_extra(action, client)
            if handled is not None:
                return handled
            print(f"跳过执行：action_type={action.action_type!r}，需手动处理")
            return False

        return True

    def _tap(self, px: float, py: float) -> bool:
        print(f"执行点击: ({px:.0f}, {py:.0f})")
        result = self._client().tap(px, py)
        print(f"结果: {result}")
        if not self._result_succeeded(result, "点击"):
            print("点击失败：跳过")
            return False
        return True

    @staticmethod
    def _result_succeeded(result: object, action_name: str = "动作") -> bool:
        """Interpret the status contract shared by device input primitives.

        A missing/non-string status is a protocol violation and fails closed.  A
        successful implementation returns an explicit status such as ``OK tap``.
        """
        if not isinstance(result, str) or not result.strip():
            print(f"{action_name}失败：设备未返回有效状态")
            return False
        low = result.lower()
        if any(marker in low for marker in ("failed", "interrupted", "paused")):
            print(f"{action_name}失败：{result}")
            return False
        return True

    # ----- hooks (platform overrides) --------------------------------------
    def _type_intercept(self, client, text: str) -> bool:
        """Platform hook called before clear+type. Return True to skip default behavior.

        Override in adapters to handle special input controls (e.g. date pickers)
        that require a non-keyboard interaction to set a value correctly."""
        return False

    def _clear_before_type(self, client, text: str) -> bool:
        """Clear the focused field before typing ``text`` (default: clear_text)."""
        print(f"  清空并输入: {self._display_value(repr(text))}")
        result = client.clear_text()
        print(f"  结果: {result}")
        return self._result_succeeded(result, "输入前清空")

    def _dispatch_extra(self, action, client) -> Optional[bool]:
        """Dispatch a platform-specific action_type. Return a bool when handled,
        None when the action_type is unknown to this platform (-> skip)."""
        return None
