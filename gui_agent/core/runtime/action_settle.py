"""Post-action settling and grounded-point helpers."""

from __future__ import annotations

import time

from gui_agent.core.schemas import BaseActionDecision
from gui_agent.core.vision.frame_analysis import (
    STABLE_MEAN_THR,
    frame_changed,
    frame_diff,
)

VERIFY_TIMEOUT_S = 8
SETTLE_FIRST_S = 1.0
SETTLE_UNIT_S = 0.5
SETTLE_MAX_UNITS = 6
SETTLE_GESTURE_FIRST_S = 0.3


def settle_after_action(
    platform: object,
    pre_frame: bytes | None,
    action_type: str | None = None,
    focus_y: float | None = None,
    center: tuple[float, float] | None = None,
) -> tuple[float, bool]:
    """Wait until the surface changes and settles, or reaches the bounded cap."""

    screenshot = getattr(platform, "settle_screenshot", platform.screenshot)
    def changed(current: bytes) -> bool:
        if pre_frame is None:
            return False
        local = frame_changed(pre_frame, current, focus_y, center=center)
        # A tap may change a keyboard/dialog outside its local target box. Type
        # remains row-only so keyboard churn cannot impersonate entered text.
        return local or center is not None and frame_changed(pre_frame, current)

    if action_type not in ("drag", "scroll", "scroll_to_ref"):
        cdp_settle = getattr(platform, "wait_settled", None)
        if cdp_settle is not None:
            try:
                elapsed, no_effect = cdp_settle(action_type)
                if no_effect and pre_frame is not None:
                    current = screenshot()
                    if changed(current):
                        no_effect = False
                return elapsed, no_effect
            except Exception as exc:  # noqa: BLE001
                print(f"  [Settle] CDP settle 异常，回退视觉: {exc}")

    started = time.perf_counter()
    if action_type in ("drag", "scroll", "scroll_to_ref"):
        previous: bytes | None = None
        for index in range(1, SETTLE_MAX_UNITS + 1):
            time.sleep(SETTLE_GESTURE_FIRST_S if index == 1 else SETTLE_UNIT_S)
            try:
                current = screenshot()
            except Exception:  # noqa: BLE001
                return time.perf_counter() - started, False
            if previous is not None and frame_diff(previous, current) < STABLE_MEAN_THR:
                no_effect = pre_frame is not None and not frame_changed(pre_frame, current)
                return time.perf_counter() - started, no_effect
            previous = current
        no_effect = (
            pre_frame is not None
            and previous is not None
            and not frame_changed(pre_frame, previous)
        )
        return time.perf_counter() - started, no_effect

    if pre_frame is None:
        time.sleep(SETTLE_FIRST_S)
        return time.perf_counter() - started, False

    previous = None
    ever_changed = False
    pop_tab = getattr(platform, "pop_tab_switched", None)
    for index in range(1, SETTLE_MAX_UNITS + 1):
        time.sleep(SETTLE_FIRST_S if index == 1 else SETTLE_UNIT_S)
        try:
            current = screenshot()
        except Exception:  # noqa: BLE001
            return time.perf_counter() - started, False
        tab_switched = bool(pop_tab and pop_tab())
        ever_changed = ever_changed or changed(current) or tab_switched
        stable = previous is not None and frame_diff(previous, current, focus_y) < STABLE_MEAN_THR
        if ever_changed and stable:
            return time.perf_counter() - started, False
        previous = current
    return time.perf_counter() - started, not ever_changed


def has_snapped_point(action_decision: BaseActionDecision | None) -> bool:
    """Return whether the executor recorded a corrected action point."""

    if action_decision is None:
        return False
    snap = getattr(action_decision.action, "snap", None)
    if not isinstance(snap, dict):
        return False
    snapped = snap.get("snapped")
    return isinstance(snapped, (list, tuple)) and len(snapped) >= 2


__all__ = ["VERIFY_TIMEOUT_S", "has_snapped_point", "settle_after_action"]
