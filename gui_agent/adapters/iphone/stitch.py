"""iPhone scroll-stitch: thin adapter over the neutral core stitch.

The stitching ALGORITHM is platform-neutral and now lives in ``gui_agent.core.vision.stitch``.
This module injects the iPhone-specific bits — the device-frame mask (rounded corners
/ 灵动岛 excluded from ORB feature detection) and the historical content band
(0.10..0.97, dropping the iOS status bar / bottom chrome) — and re-exports the same
names (``StitchAccumulator`` / ``robust_shift`` / ``_gray_u8``) so existing iphone
callers (factory, scroll_probe, runner type hints) are unchanged and behavior is
byte-identical to before the move.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from gui_agent.core.vision import stitch as _core
from gui_agent.core.vision.stitch import _gray_u8  # neutral passthrough (re-exported)

_MASK_PATH = Path(__file__).parent / "assets" / "mcp_frame_mask.png"
# 与 perception 同约定：0=屏幕内容、255=设备框（圆角/灵动岛/边框）
_MASK = np.asarray(Image.open(_MASK_PATH).convert("L")) if _MASK_PATH.exists() else None

# iPhone 历史内容带（= core 默认值，显式写出以自描述这是 iphone 标定）。
_TOP, _BOT = _core.CONTENT_TOP, _core.CONTENT_BOT

__all__ = ["StitchAccumulator", "robust_shift", "_gray_u8"]


def robust_shift(prev_u8: np.ndarray, cur_u8: np.ndarray) -> tuple[int, float]:
    """iPhone 垂直位移估计：core.robust_shift + 设备框 mask + iphone 内容带。"""
    return _core.robust_shift(prev_u8, cur_u8, content_top=_TOP, content_bot=_BOT, frame_mask=_MASK)


class StitchAccumulator(_core.StitchAccumulator):
    """core StitchAccumulator with the iPhone device-frame mask + content band injected."""

    def __init__(self, **kwargs) -> None:
        kwargs.setdefault("content_top", _TOP)
        kwargs.setdefault("content_bot", _BOT)
        kwargs.setdefault("frame_mask", _MASK)
        super().__init__(**kwargs)
