"""Post-action effect detection: structural + color, not grayscale mean.

Regression for logs/.../android/20260611_085000: the 闹钟 tab switch had a whole-frame
grayscale mean diff of only ~6.1 (< the old 8.0 effect threshold) and was wrongly flagged
no_effect, even though the page clearly changed. ``frame_changed`` should identify that
diluted-but-real change while staying conservative on identical or tiny-noise frames.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from PIL import Image, ImageDraw
import pytest

from gui_agent.core.vision.frame_analysis import frame_changed, frame_diff

FIXTURES = Path(__file__).parent / "fixtures" / "frame_analysis"


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _load_cases() -> list[dict]:
    return json.loads((FIXTURES / "cases.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", _load_cases(), ids=lambda c: c["label"])
def test_frame_changed_fixture_cases(case: dict):
    before = (FIXTURES / case["before"]).read_bytes()
    after = (FIXTURES / case["after"]).read_bytes()

    if "max_gray_mean_diff" in case:
        assert frame_diff(before, after) < case["max_gray_mean_diff"]
    assert frame_changed(before, after) is case["expected_changed"]


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
