"""Two-tier (局部 + 全局) screen-stuck detection for pickers / value adjustments.

Regression for the android alarm runs (logs/.../android/2026061021*) where a wheel time
picker false-tripped the stuck detector: the picker changes only a SMALL region (whole-
frame similarity stays >99%) and you repeat the same column scroll. The fix judges "no
change" only when BOTH the whole frame stayed similar AND the region the action touched
(tap/scroll/drag all carry x/y) did not move; and it suppresses instruction-repetition
stuck for value adjustments once the screen is confirmed to be moving.

Locks the metric ``region_change`` and the predicates ``_action_center`` / ``_is_value_adjust``.
"""

from __future__ import annotations

import io
import types

from PIL import Image, ImageDraw

from gui_agent.core.vision.frame_analysis import CHANGE_SSIM_DIST_THR, region_change
from gui_agent.core.supervisor.statement.policy import StatementSupervisorPolicy
from gui_agent.core.run.progress_monitor import ProgressMonitor


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ── 局部 tier: region_change(global_sim, local 1-SSIM in action region) ──────────
def test_region_change_local_region_catches_small_change_global_misses_it():
    base = Image.new("L", (256, 256), 255)
    mod = base.copy()
    ImageDraw.Draw(mod).rectangle([100, 100, 130, 130], fill=0)  # one small region changed
    p1, p2 = _png(base), _png(mod)

    # Whole frame barely changed (a tiny region) -> global similarity stays high...
    gs, loc_here = region_change(p1, p2, center=(100 / 256 * 1000, 115 / 256 * 1000))
    assert gs > 0.95
    # ...but the region the action TOUCHED structurally changed (1-SSIM well over the bar).
    assert loc_here is not None and loc_here > CHANGE_SSIM_DIST_THR


def test_region_change_region_far_from_change_sees_nothing():
    base = Image.new("L", (256, 256), 255)
    mod = base.copy()
    ImageDraw.Draw(mod).rectangle([100, 100, 130, 130], fill=0)
    gs, loc_far = region_change(_png(base), _png(mod), center=(10, 10))  # top-left, far away
    assert loc_far is not None and loc_far < CHANGE_SSIM_DIST_THR  # reads as "no change"


def test_region_change_no_center_returns_global_only():
    base = Image.new("L", (256, 256), 255)
    gs, loc = region_change(_png(base), _png(base), center=None)
    assert loc is None  # coordless action -> caller falls back to the global tier


def test_region_change_identical_frames_no_change_anywhere():
    base = _png(Image.new("L", (256, 256), 255))
    gs, loc = region_change(base, base, center=(500, 500))
    assert gs > 0.999 and loc == 0.0


# ── _action_center: where to look (tap/scroll/drag carry x/y) ──────────────────
def test_action_center_from_coordinates():
    f = ProgressMonitor.action_center
    assert f(types.SimpleNamespace(x=500.0, y=250.0)) == (500.0, 250.0)


def test_action_center_none_for_coordless_and_missing():
    f = ProgressMonitor.action_center
    assert f(types.SimpleNamespace(x=None, y=None)) is None  # press_enter / home / back
    assert f(None) is None


# ── _is_value_adjust: repeating the same column scroll is normal, not a loop ────
def test_is_value_adjust_scroll_and_picker_drag():
    f = ProgressMonitor.is_value_adjust
    assert f(types.SimpleNamespace(action_type="scroll")) is True  # android picker
    assert f(types.SimpleNamespace(action_type="drag", target_area="picker_hour")) is True  # iphone


def test_is_value_adjust_false_for_tap_plain_drag_none():
    f = ProgressMonitor.is_value_adjust
    assert f(types.SimpleNamespace(action_type="tap")) is False
    assert f(types.SimpleNamespace(action_type="drag", target_area="")) is False
    assert f(None) is False
