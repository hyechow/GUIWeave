"""Translate semantic scroll actions into concrete iPhone gestures."""

from __future__ import annotations

from dataclasses import dataclass

from policy_expr.executor_constants import SCROLL_DELTA, SCROLL_TICKS
from policy_expr.schemas import Action


@dataclass(frozen=True)
class WheelGesture:
    x: float
    y: float
    direction: str
    ticks: int
    delta_px: int


@dataclass(frozen=True)
class DragGesture:
    x: float
    y: float
    to_x: float
    to_y: float
    duration_ms: int


_AREA_POINTS: dict[str, tuple[float, float]] = {
    "main_content": (500.0, 760.0),
    "left_panel": (260.0, 760.0),
    "right_panel": (740.0, 760.0),
    "top_content": (500.0, 420.0),
    "bottom_content": (500.0, 820.0),
    "picker_left": (270.0, 635.0),
    "picker_center": (500.0, 635.0),
    "picker_right": (730.0, 635.0),
}

_WHEEL_AMOUNT: dict[str, tuple[int, int]] = {
    "small": (1, 100),
    "medium": (2, 120),
    "large": (3, 140),
}

_DRAG_AMOUNT: dict[str, tuple[float, int]] = {
    "small": (160.0, 800),
    "medium": (320.0, 900),
    "large": (500.0, 1100),
}

# picker 滚轮：减小步长 + 降速以抑制 fling 惯性。实测旧 medium(180px) 一拨 ~8 格，
# 离目标 1-3 格时必然冲过头来回震荡。缩短距离让步长更细，拉长时长降低抬手末速度
# （fling 与末速度正相关），轮子跟手、抬手即停，既少冲过头也更快停稳。
# 注：px→格 系数随机型/页面变化，下列为起始值，按实跑日志校准。
_PICKER_DRAG_AMOUNT: dict[str, tuple[float, int]] = {
    "small": (40.0, 850),
    "medium": (100.0, 1000),
    "large": (220.0, 1300),
}


def wheel_gesture(action: Action) -> WheelGesture:
    x, y = _anchor_point(action)
    ticks, delta_px = _WHEEL_AMOUNT.get(action.amount, _WHEEL_AMOUNT["medium"])
    return WheelGesture(
        x=x,
        y=y,
        direction=_effective_content_direction(action),
        ticks=ticks,
        delta_px=delta_px,
    )


def drag_gesture(action: Action) -> DragGesture:
    x, y = _anchor_point(action)
    amount_map = _PICKER_DRAG_AMOUNT if _is_picker_area(action.target_area) else _DRAG_AMOUNT
    distance, duration_ms = amount_map.get(action.amount, amount_map["medium"])
    direction = _effective_content_direction(action)
    dx, dy = _drag_delta(direction, distance)
    return DragGesture(
        x=x,
        y=y,
        to_x=max(30.0, min(970.0, x + dx)),
        to_y=max(200.0, min(850.0, y + dy)),
        duration_ms=duration_ms,
    )


def action_with_wheel(action: Action, gesture: WheelGesture) -> Action:
    return action.model_copy(
        update={
            "x": gesture.x,
            "y": gesture.y,
            "direction": gesture.direction,
        }
    )


def action_with_drag(action: Action, gesture: DragGesture) -> Action:
    return action.model_copy(
        update={
            "action_type": "drag",
            "x": gesture.x,
            "y": gesture.y,
            "to_x": gesture.to_x,
            "to_y": gesture.to_y,
            "duration_ms": gesture.duration_ms,
            "direction": _effective_content_direction(action),
        }
    )


def _normalized_direction(direction: str | None) -> str:
    value = (direction or "down").strip().lower()
    aliases = {
        "向下": "down",
        "downward": "down",
        "向上": "up",
        "upward": "up",
        "向左": "left",
        "leftward": "left",
        "向右": "right",
        "rightward": "right",
    }
    return aliases.get(value, value)


def _effective_content_direction(action: Action) -> str:
    if _is_picker_area(action.target_area) and action.value_direction:
        # Picker rule observed in iPhone Mirroring:
        # drag up reveals larger values; drag down reveals smaller values.
        # _drag_delta interprets "down" as reveal lower/later content -> finger up.
        return "down" if action.value_direction == "increase" else "up"
    return _normalized_direction(action.direction)


def _anchor_point(action: Action) -> tuple[float, float]:
    default_x, default_y = _AREA_POINTS.get(action.target_area, _AREA_POINTS["main_content"])
    if _is_picker_area(action.target_area):
        # For picker wheels, the model may point at labels or the selected-value
        # display instead of the draggable wheel band. Keep the column x if it
        # supplied one, but always use the calibrated selected-row y.
        x = action.x if action.x is not None else default_x
        y = default_y
    else:
        x = action.x if action.x is not None else default_x
        y = action.y if action.y is not None else default_y
    return max(30.0, min(970.0, float(x))), max(200.0, min(850.0, float(y)))


def _is_picker_area(target_area: str) -> bool:
    return target_area.startswith("picker_")


def _drag_delta(direction: str, distance: float) -> tuple[float, float]:
    # direction describes the content the user wants to reveal.
    if direction == "down":
        return 0.0, -distance
    if direction == "up":
        return 0.0, distance
    if direction == "left":
        return -distance, 0.0
    if direction == "right":
        return distance, 0.0
    return 0.0, -distance
