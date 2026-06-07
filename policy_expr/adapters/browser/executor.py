"""Browser action executor: dispatch one ActionDecision onto the page.

``BrowserExecutor.execute`` matches the iphone ``ActionExecutor.execute`` signature
EXACTLY — ``execute(decision, app_name='', png_bytes=None, is_home_screen=False)
-> bool`` — so the generic runner loop drives it the same way (the trailing
iphone-only args are accepted and ignored).

Coordinates ``action.x`` / ``action.y`` are normalized 0-1000 over the viewport;
they are denormalized to viewport pixels via the device's ``viewport_size`` (the
browser analogue of the iphone ``logical_xy`` / WIN_W / WIN_H step). Intentionally
SIMPLE: NO YOLO snap, NO OCR, NO picker.
"""

from __future__ import annotations

import time

from policy_expr.core.schemas import ActionDecision


class BrowserExecutor:
    """Execute normalized policy actions against the browser via PlaywrightDevice."""

    def __init__(self, session):
        # ``session`` is a BrowserSession; the device lives on ``.client``.
        self.session = session

    def _client(self):
        client = getattr(self.session, "client", None)
        if client is None:
            raise RuntimeError("浏览器尚未连接")
        return client

    def _denorm(self, ax: float, ay: float) -> tuple[float, float]:
        """Normalized 0-1000 -> viewport pixels, clamped inside the viewport."""
        w, h = self._client().viewport_size
        x = ax / 1000 * w
        y = ay / 1000 * h
        return max(0.0, min(x, w - 1)), max(0.0, min(y, h - 1))

    def prepare_frame(self, png_bytes: bytes) -> None:
        """No-op: the browser adapter is vision-only (no YOLO/OCR tap snapping).
        The runner submits this to a background thread each turn; iphone uses it to
        precompute snap data — browser has nothing to precompute."""
        return None

    def execute_scroll(self, action, *, ticks: int = 0, delta_px: int = 0) -> None:
        """Scroll without execute()'s bool wrapper (runner scroll-cache path).
        ``ticks`` / ``delta_px`` are iphone scroll-probe params and are ignored —
        browser scroll is deterministic via the device wheel."""
        client = self._client()
        ax = action.x if action.x is not None else 500
        ay = action.y if action.y is not None else 500
        px, py = self._denorm(ax, ay)
        amount = _amount_to_units(action.amount)
        direction = action.direction or "down"
        print(f"  scroll {direction} amount={amount} @({px:.0f},{py:.0f})")
        print(f"  结果: {client.scroll(direction, amount, px, py)}")

    def execute(
        self,
        decision: ActionDecision,
        app_name: str = "",
        png_bytes: bytes | None = None,
        is_home_screen: bool = False,
    ) -> bool:
        # app_name / png_bytes / is_home_screen are iphone-only; ignored here.
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
            # Replace existing contents: select-all + type the new text.
            print(f"  清空并输入: {action.text!r}")
            print(f"  结果: {client.select_all()}")
            print(f"  结果: {client.type_text(action.text)}")

        elif action.action_type == "clear_text":
            print("清空当前输入框")
            print(f"  结果: {client.clear_text()}")

        elif action.action_type == "press_enter":
            print("按回车确认输入")
            print(f"  结果: {client.press_enter()}")

        elif action.action_type == "scroll" and action.direction:
            ax = action.x if action.x is not None else 500
            ay = action.y if action.y is not None else 500
            px, py = self._denorm(ax, ay)
            amount = _amount_to_units(action.amount)
            print(f"  scroll {action.direction} amount={amount} @({px:.0f},{py:.0f})")
            print(f"  结果: {client.scroll(action.direction, amount, px, py)}")

        elif action.action_type == "drag":
            if action.x is None or action.y is None or action.to_x is None or action.to_y is None:
                print("拖动失败：缺少 x/y/to_x/to_y")
                return False
            fx, fy = self._denorm(action.x, action.y)
            tx, ty = self._denorm(action.to_x, action.to_y)
            duration_ms = action.duration_ms or 1000
            print(f"  drag ({fx:.0f},{fy:.0f})->({tx:.0f},{ty:.0f}), {duration_ms}ms")
            print(f"  结果: {client.drag(fx, fy, tx, ty, duration_ms)}")

        elif action.action_type == "home":
            print("执行返回起始页")
            print(f"  结果: {client.press_home()}")

        elif action.action_type == "stop":
            print("停止操作（当前状态已满足目标，无需执行）")
            return True

        else:
            print(f"跳过执行：action_type={action.action_type!r}，需手动处理")
            return False

        return True

    def _tap(self, px: float, py: float) -> bool:
        print(f"执行点击: ({px:.0f}, {py:.0f})")
        result = self._client().tap(px, py)
        print(f"结果: {result}")
        low = result.lower()
        if "interrupted" in low or "failed" in low:
            print("点击失败：跳过")
            return False
        return True


# Map the schema's coarse ScrollAmount onto PlaywrightDevice.scroll amount units.
# (PlaywrightDevice scrolls amount * 120px; 3/5/9 ≈ small/medium/large screen-fracs.)
_AMOUNT_UNITS = {"small": 3, "medium": 5, "large": 9}


def _amount_to_units(amount: str) -> int:
    return _AMOUNT_UNITS.get(amount, 5)
