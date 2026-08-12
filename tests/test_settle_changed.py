"""Post-action effect detection: structural + color, not grayscale mean.

Regression for logs/.../android/20260611_085000: the 闹钟 tab switch had a whole-frame
grayscale mean diff of only ~6.1 (< the old 8.0 effect threshold) and was wrongly flagged
no_effect, even though the page clearly changed. ``frame_changed`` should identify that
diluted-but-real change while staying conservative on identical or tiny-noise frames.
"""

from __future__ import annotations

import io

from PIL import Image, ImageDraw

from gui_agent.core.runtime.action_settle import settle_after_action
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


def test_settle_uses_adapter_lightweight_screenshot_probe(monkeypatch):
    frame = _png(Image.new("RGB", (160, 320), "white"))

    class _Platform:
        def __init__(self):
            self.probe_calls = 0
            self.full_calls = 0

        def settle_screenshot(self):
            self.probe_calls += 1
            return frame

        def screenshot(self):
            self.full_calls += 1
            raise AssertionError("settle should not run the recovery-heavy capture")

    platform = _Platform()
    monkeypatch.setattr("gui_agent.core.runtime.action_settle.time.sleep", lambda _: None)

    _, no_effect = settle_after_action(platform, frame, action_type="tap")

    assert no_effect is True
    assert platform.probe_calls == 6
    assert platform.full_calls == 0
