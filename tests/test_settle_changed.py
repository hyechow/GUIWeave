"""Post-action effect detection: structural + color, not grayscale mean.

Regression for logs/.../android/20260611_085000: the 闹钟 tab switch had a whole-frame
grayscale mean diff of only ~6.1 (< the old 8.0 effect threshold) and was wrongly flagged
no_effect, even though the page clearly changed. ``frame_changed`` should identify that
diluted-but-real change while staying conservative on identical or tiny-noise frames.
"""

from __future__ import annotations

import io

from PIL import Image, ImageDraw

from gui_agent.core.vision.frame_analysis import frame_changed, frame_diff


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_region_color_change_is_changed():
    # a real page change: a large region swaps content/color (analogue of the tab switch)
    base = Image.new("RGB", (160, 320), "white")
    mod = base.copy()
    ImageDraw.Draw(mod).rectangle([0, 160, 160, 320], fill=(40, 90, 200))  # bottom half -> blue
    assert frame_changed(_png(base), _png(mod)) is True


def test_tiny_change_stays_not_changed_conservative():
    # a few-pixel blip must NOT trip "changed" (stays conservative -> no_effect only when truly static)
    base = Image.new("RGB", (160, 320), "white")
    mod = base.copy()
    ImageDraw.Draw(mod).rectangle([2, 2, 6, 6], fill="black")
    assert frame_changed(_png(base), _png(mod)) is False
