"""frame_changed center-box: a tap's LOCAL effect is detected even when too small to move the
whole-frame signals.

Regression for logs/.../browser/20260611_135605 turn 2: clicking the left-nav 订单 menu expanded
its submenu (a small change in the narrow sidebar). The settle effect-detector compared the WHOLE
frame (focus only passed for `type`), so all three signals stayed under threshold (ssim_dist 0.026
< 0.08) → false no_effect → a needless replan that forbade a working instruction. Fix: tap passes
its click point; frame_changed crops the box around it and the SAME SSIM+color signals fire on the
localized change. These use a synthetic localized block so the test is deterministic / CI-safe
(no gitignored frames).
"""

from __future__ import annotations

import io

import numpy as np
from PIL import Image

from gui_agent.core.vision.frame_analysis import frame_changed


def _png(arr: np.ndarray) -> bytes:
    out = io.BytesIO()
    Image.fromarray(arr, "RGB").save(out, format="PNG")
    return out.getvalue()


def _frames() -> tuple[bytes, bytes]:
    """Two textured frames identical except a ~2% localized block (≈ a sidebar submenu expanding)."""
    rng = np.random.default_rng(0)
    base = rng.integers(60, 200, size=(640, 320, 3), dtype=np.uint8)  # texture → SSIM is meaningful
    a = base.copy()
    b = base.copy()
    b[150:230, 10:60] = 240  # ~80x50 of 640x320 ≈ 2% of pixels, in the left band
    return _png(a), _png(b)


def test_whole_frame_misses_localized_change():
    a, b = _frames()
    assert frame_changed(a, b) is False  # diluted across the whole frame → looks unchanged


def test_center_box_catches_localized_change():
    a, b = _frames()
    # click point over the changed block, normalized 0-1000: x≈30/320*1000, y≈190/640*1000
    assert frame_changed(a, b, center=(94, 297)) is True


def test_center_box_away_from_change_stays_false():
    a, b = _frames()
    assert frame_changed(a, b, center=(800, 800)) is False  # box elsewhere sees nothing
