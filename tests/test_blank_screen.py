"""Deterministic regression tests for _is_blank_screen (loading-screen guard).

Regression: log 20260605_224604 turn 6. The WeChat 账单 page was still loading
(thin green progress bar + empty list), but the guard missed it because:
  - the blank body renders ~gray 239, so the old `pixel > 250` count found nothing;
  - the black iPhone-Mirroring bezel diluted the whole-image ratio below the cutoff.
The fix de-bezels, drops the top chrome, and treats the body as blank when it's
light AND near-uniform (mean >= 225, stddev <= 12).

Fixtures are SYNTHETIC images built in-code (per the no-images-in-git rule), sized
and shaded to reproduce the measured turn-6 characteristics: a gray-239 body inside
a black bezel, with a top chrome strip (header text / progress bar / toolbar).

Run: `uv run pytest tests/test_blank_screen.py`
"""

import io

import pytest
from PIL import Image, ImageDraw

from policy_expr.core.supervisor.milestone.policy import _is_blank_screen

W, H = 600, 1300


def _make_png(body_fill: int, *, textured: bool = False, with_bezel: bool = True) -> bytes:
    """Build a framed grayscale screenshot.

    body_fill: brightness of the content body (239 = this app's "blank" gray).
    textured:  draw a rendered list (icons/text/amounts) over the body.
    with_bezel: surround the screen with a black bezel (the Mirroring frame).
    """
    img = Image.new("L", (W, H), 0 if with_bezel else body_fill)
    d = ImageDraw.Draw(img)
    inset = 24 if with_bezel else 0
    top, bot = (70, H - 70) if with_bezel else (0, H)
    sx0, sx1 = inset, W - inset
    d.rectangle([sx0, top, sx1, bot], fill=body_fill)
    # Top chrome (well within the top ~12%): header text, progress bar, toolbar pill.
    chrome_bottom = top + int((bot - top) * 0.12)
    d.text((W // 2 - 20, top + 24), "账单", fill=20)
    d.line([(sx0, top + 52), (sx0 + 180, top + 52)], fill=120, width=3)  # progress bar
    d.rounded_rectangle([sx0 + 20, chrome_bottom - 28, sx0 + 140, chrome_bottom],
                        radius=8, fill=205, outline=120)
    if textured:
        y = chrome_bottom + 40
        while y < bot - 40:
            d.ellipse([sx0 + 20, y, sx0 + 60, y + 40], fill=90)        # row icon
            d.rectangle([sx0 + 80, y + 6, sx0 + 300, y + 18], fill=40)  # merchant text
            d.rectangle([sx1 - 120, y + 6, sx1 - 30, y + 22], fill=30)  # amount
            y += 90
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# (id, png-bytes builder, expected _is_blank_screen)
CASES = [
    ("gray239_body_blank",       _make_png(239),                         True),   # the turn-6 case
    ("near_white_blank",         _make_png(252),                         True),
    ("blank_no_bezel",           _make_png(239, with_bezel=False),       True),
    ("rendered_list_not_blank",  _make_png(239, textured=True),          False),  # real content
    ("dark_page_not_blank",      _make_png(60),                          False),  # splash/dark mode
]


@pytest.mark.parametrize("png,expected", [(c[1], c[2]) for c in CASES], ids=[c[0] for c in CASES])
def test_is_blank_screen(png, expected):
    assert _is_blank_screen(png) is expected
