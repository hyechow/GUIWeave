from __future__ import annotations

import io

from PIL import Image, ImageDraw

from gui_agent.core.vision.frame_analysis import visual_surface_fingerprint


def _png(image: Image.Image) -> bytes:
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_visual_surface_fingerprint_tracks_content_not_system_bars() -> None:
    first = Image.new("RGB", (200, 400), "white")
    second = first.copy()
    ImageDraw.Draw(first).rectangle([0, 0, 199, 23], fill="red")
    ImageDraw.Draw(second).rectangle([0, 0, 199, 23], fill="blue")
    ImageDraw.Draw(first).rectangle([0, 384, 199, 399], fill="black")
    ImageDraw.Draw(second).rectangle([0, 384, 199, 399], fill="green")

    baseline = visual_surface_fingerprint(_png(first))
    assert baseline == visual_surface_fingerprint(_png(second))
    ImageDraw.Draw(second).rectangle([30, 100, 170, 220], fill="black")
    assert baseline != visual_surface_fingerprint(_png(second))
