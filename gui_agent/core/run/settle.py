"""Screen settling and post-action point helpers."""

from __future__ import annotations

import time

from gui_agent.core.schemas import ActionDecision
from gui_agent.core.vision.frame_analysis import STABLE_MEAN_THR, frame_changed, frame_diff

SETTLE_FIRST_S = 1.0
SETTLE_UNIT_S = 0.5
SETTLE_MAX_UNITS = 6
SETTLE_GESTURE_FIRST_S = 0.3


def settle_after_action(
    phone: object,
    pre_frame: bytes | None,
    action_type: str | None = None,
    focus_y: float | None = None,
    center: tuple[float, float] | None = None,
) -> tuple[float, bool]:
    """Wait until the screen changed and settled, or hit the cap.

    Returns `(elapsed_seconds, no_effect)`. Browser devices may provide their own
    CDP-based settle; drag/scroll stays visual because smooth scrolling can settle
    in the DOM before visual inertia has ended.
    """
    if action_type not in ("drag", "scroll"):
        cdp_settle = getattr(phone, "wait_settled", None)
        if cdp_settle is not None:
            try:
                return cdp_settle(action_type)
            except Exception as exc:
                print(f"  [Settle] CDP settle 异常，回退视觉: {exc}")
    started = time.perf_counter()
    if action_type in ("drag", "scroll"):
        prev: bytes | None = None
        for i in range(1, SETTLE_MAX_UNITS + 1):
            time.sleep(SETTLE_GESTURE_FIRST_S if i == 1 else SETTLE_UNIT_S)
            try:
                cur = phone.screenshot()
            except Exception:
                elapsed = time.perf_counter() - started
                print(f"  [Settle] {elapsed:.1f}s ({i} 轮，截图异常提前返回)")
                return elapsed, False
            if prev is not None and frame_diff(prev, cur) < STABLE_MEAN_THR:
                elapsed = time.perf_counter() - started
                print(f"  [Settle] {elapsed:.1f}s ({i} 轮，停稳: {action_type})")
                return elapsed, False
            prev = cur
        elapsed = time.perf_counter() - started
        print(f"  [Settle] {elapsed:.1f}s ({SETTLE_MAX_UNITS} 轮，达上限: {action_type})")
        return elapsed, False
    if pre_frame is None:
        time.sleep(SETTLE_FIRST_S)
        elapsed = time.perf_counter() - started
        print(f"  [Settle] {elapsed:.1f}s (无动作前帧)")
        return elapsed, False
    prev: bytes | None = None
    ever_changed = False
    pop_tab = getattr(phone, "pop_tab_switched", None)
    for i in range(1, SETTLE_MAX_UNITS + 1):
        time.sleep(SETTLE_FIRST_S if i == 1 else SETTLE_UNIT_S)
        try:
            cur = phone.screenshot()
        except Exception:
            elapsed = time.perf_counter() - started
            print(f"  [Settle] {elapsed:.1f}s ({i} 轮，截图异常提前返回)")
            return elapsed, False
        tab_just_switched = bool(pop_tab and pop_tab())
        if tab_just_switched:
            ever_changed = True
            print(f"  [Settle] {time.perf_counter() - started:.1f}s ({i} 轮，tab切换→有效果)")
        changed = frame_changed(pre_frame, cur, focus_y, center=center)
        ever_changed = ever_changed or changed
        stable = prev is not None and frame_diff(prev, cur, focus_y) < STABLE_MEAN_THR
        if (changed or tab_just_switched) and stable:
            elapsed = time.perf_counter() - started
            print(f"  [Settle] {elapsed:.1f}s ({i} 轮，变过且停稳)")
            return elapsed, False
        prev = cur
    elapsed = time.perf_counter() - started
    no_effect = not ever_changed
    tag = "达上限·零效果" if no_effect else "达上限"
    print(f"  [Settle] {elapsed:.1f}s ({SETTLE_MAX_UNITS} 轮，{tag})")
    return elapsed, no_effect


def snapped_point(action_decision: ActionDecision | None) -> tuple[float, float] | None:
    """Return the actual tap location, snapped if DOM/vision snapping fired."""
    if action_decision is None:
        return None
    action = action_decision.action
    if action.action_type not in ("tap", "click") or action.x is None or action.y is None:
        return None
    snap = action.snap
    if snap and snap.get("snapped"):
        sx, sy = snap["snapped"]
        return float(sx), float(sy)
    return float(action.x), float(action.y)
