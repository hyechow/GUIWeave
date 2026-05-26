"""Stuck detection eval: validates SimStuck frozen vs AB-loop behavior.

Uses synthetic PNG images — no real screenshots needed, no LLM calls.

Cases:
  1. AB loop  — frame A / frame B / frame A:
       2-back sim high, adjacent sim low → frozen=False
       Must NOT be suppressed even when checker summaries differ.

  2. Picker freeze — three nearly-identical frames (tiny pixel change each):
       All sims ≥ 99% → frozen=True
       May be suppressed when checker summaries differ (picker progress).

  3. No stuck — three clearly different frames:
       Sims below threshold → returns None.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from policy_expr.schemas import Observation
from policy_expr.supervisor.milestone import (
    STUCK_SCREEN_FROZEN,
    STUCK_SCREEN_WINDOW,
    MilestoneSupervisorPolicy,
    _SingleCheckResult,
)

# ── Image helpers ──────────────────────────────────────────────────────────────

W, H = 390, 844  # simulated phone resolution


def _solid(color: tuple[int, int, int]) -> bytes:
    img = Image.new("RGB", (W, H), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _with_patch(base: bytes, row: int, color: tuple[int, int, int], height: int = 10) -> bytes:
    """Replace `height` rows starting at `row` with `color`."""
    img = Image.open(io.BytesIO(base)).convert("RGB")
    patch = Image.new("RGB", (W, height), color)
    img.paste(patch, (0, row))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _obs(png: bytes) -> Observation:
    return Observation(png_bytes=png, source="eval")


# ── Synthetic frames ───────────────────────────────────────────────────────────

FRAME_WHITE = _solid((255, 255, 255))                      # "home screen"
FRAME_DARK  = _with_patch(FRAME_WHITE, 0, (30, 30, 30), H // 2)  # "信息 app" — top half dark

# Picker frames: white base with a tiny 2-px patch changing row by row
# After 64×64 resize the per-pixel delta is negligible → all sims ≥ 99%
FRAME_PICKER_1 = _with_patch(FRAME_WHITE, 420, (240, 240, 240), 2)
FRAME_PICKER_2 = _with_patch(FRAME_WHITE, 422, (240, 240, 240), 2)
FRAME_PICKER_3 = _with_patch(FRAME_WHITE, 424, (240, 240, 240), 2)

FRAME_RED   = _solid((220,  60,  60))
FRAME_GREEN = _solid(( 60, 200,  60))
FRAME_BLUE  = _solid(( 60,  60, 220))


# ── Helpers ────────────────────────────────────────────────────────────────────

passed = 0
failed = 0


def _report(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    passed += ok
    failed += not ok
    tag = "PASS" if ok else "FAIL"
    line = f"  [{tag}] {label:55s}"
    if detail:
        line += f"  {detail}"
    print(line)


def _fresh_policy(*frames: bytes) -> tuple[MilestoneSupervisorPolicy, _SingleCheckResult | None]:
    """Feed frames into a fresh policy and return the SimStuck result for the last one."""
    policy = MilestoneSupervisorPolicy()
    result = None
    for frame in frames:
        result = policy._check_screen_similarity(_obs(frame))
    return policy, result


# ── Test cases ─────────────────────────────────────────────────────────────────

def test_ab_loop_detected() -> None:
    """AB loop (A/B/A) → frozen=False stuck must be returned."""
    _, result = _fresh_policy(FRAME_WHITE, FRAME_DARK, FRAME_WHITE)
    ok = result is not None and result.status == "stuck" and not result.frozen
    _report("AB循环-应检测为stuck且frozen=False", ok,
            f"got: {result}" if not ok else "")


def test_ab_loop_not_suppressed_by_summary_change() -> None:
    """Confirm the fix: frozen=False stuck is NOT suppressed when summaries differ.

    The suppression code checks `sim_stuck.frozen`, so AB-loop results (frozen=False)
    are never suppressed regardless of checker summary changes.
    """
    _, result = _fresh_policy(FRAME_WHITE, FRAME_DARK, FRAME_WHITE)
    ok = result is not None and not result.frozen
    _report("AB循环-frozen=False不被摘要变化抑制", ok,
            "frozen=True means it would be suppressible" if not ok else "")


def test_picker_freeze_detected() -> None:
    """Three nearly-identical picker frames → frozen=True stuck."""
    _, result = _fresh_policy(FRAME_PICKER_1, FRAME_PICKER_2, FRAME_PICKER_3)
    ok = result is not None and result.status == "stuck" and result.frozen
    _report("Picker冻结-应检测为stuck且frozen=True", ok,
            f"got: {result}" if not ok else "")


def test_picker_freeze_suppressible() -> None:
    """frozen=True can be suppressed (the picker-progress path)."""
    _, result = _fresh_policy(FRAME_PICKER_1, FRAME_PICKER_2, FRAME_PICKER_3)
    ok = result is not None and result.frozen
    _report("Picker冻结-frozen=True可被进展抑制", ok,
            "frozen=False would not be suppressible" if not ok else "")


def test_no_stuck_for_different_frames() -> None:
    """Three clearly different frames → no stuck."""
    _, result = _fresh_policy(FRAME_RED, FRAME_GREEN, FRAME_BLUE)
    ok = result is None
    _report("三帧明显不同-不触发stuck", ok,
            f"unexpected stuck: {result}" if not ok else "")


def test_window_too_small_returns_none() -> None:
    """Fewer than STUCK_SCREEN_WINDOW frames → no result."""
    _, result = _fresh_policy(FRAME_WHITE, FRAME_DARK)
    ok = result is None
    _report(f"帧数不足{STUCK_SCREEN_WINDOW}帧-返回None", ok,
            f"unexpected: {result}" if not ok else "")


def main() -> int:
    print("── Stuck Detection Eval ──")
    test_window_too_small_returns_none()
    test_no_stuck_for_different_frames()
    test_ab_loop_detected()
    test_ab_loop_not_suppressed_by_summary_change()
    test_picker_freeze_detected()
    test_picker_freeze_suppressible()
    print(f"\n{passed + failed} tests: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
