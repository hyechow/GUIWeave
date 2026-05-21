"""Shared visualization helpers for recon test scripts."""

from __future__ import annotations

import io
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

PALETTE = [
    (255, 59, 48),  (255, 149, 0),  (255, 204, 0),  (52, 199, 89),
    (0, 199, 190),  (50, 173, 230), (0, 122, 255),  (88, 86, 214),
    (175, 82, 222), (255, 45, 85),
]


def annotate(
    png: bytes,
    items: list[tuple[float, float, str, str]],
    visited: set[int] | None = None,
) -> bytes:
    """Draw numbered circles on screenshot; dim visited items. Return annotated PNG."""
    img = Image.open(io.BytesIO(png)).convert("RGB")
    w, h = img.size
    draw = ImageDraw.Draw(img, "RGBA")

    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", size=22)
    except Exception:
        font = ImageFont.load_default()

    R = 18
    for i, (ax, ay, *_) in enumerate(items):
        cx = int(ax / 1000 * w)
        cy = int(ay / 1000 * h)
        color = PALETTE[i % len(PALETTE)]
        alpha = 100 if (visited and i in visited) else 230
        draw.ellipse([cx - R, cy - R, cx + R, cy + R],
                     fill=(*color, alpha), outline=(255, 255, 255, 255), width=2)
        num_str = str(i)
        bb = draw.textbbox((0, 0), num_str, font=font)
        tw, th = bb[2] - bb[0], bb[3] - bb[1]
        draw.text((cx - tw / 2, cy - th / 2 - 1), num_str,
                  fill=(255, 255, 255), font=font)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def open_annotated(
    png: bytes,
    items: list[tuple[float, float, str, str]],
    stem: str,
    visited: set[int] | None = None,
    zoom_bbox: tuple[int, int, int, int] | None = None,
) -> Path:
    """Annotate, optionally crop to zoom_bbox, save to /tmp and open in Preview."""
    annotated = annotate(png, items, visited)
    if zoom_bbox:
        full = Image.open(io.BytesIO(annotated))
        x1, y1, x2, y2 = zoom_bbox
        pad = 60
        W, H = full.size
        region = (max(0, x1 - pad), max(0, y1 - pad),
                  min(W, x2 + pad), min(H, y2 + pad))
        buf = io.BytesIO()
        full.crop(region).save(buf, format="PNG")
        annotated = buf.getvalue()
    ts = datetime.now().strftime("%H%M%S")
    out = Path(tempfile.gettempdir()) / f"{stem}_{ts}.png"
    out.write_bytes(annotated)
    subprocess.Popen(["open", str(out)])
    return out


def parse_items(
    png: bytes,
    parser,
    filter_back: bool = False,
) -> list[tuple[float, float, str, str]]:
    """Parse interactive elements from screenshot. Returns (ax, ay, label, etype) tuples."""
    from policy_expr.recon.page_parser import classify_elements
    areas = classify_elements(parser.parse_screen(png))
    if filter_back:
        areas = [a for a in areas if a.element_type != "back_button"]
    return [(a.center_xy[0], a.center_xy[1], a.label[:30] or "(无标签)", a.element_type)
            for a in areas]


def print_items(
    items: list[tuple[float, float, str, str]],
    visited: set[int] | None = None,
) -> None:
    """Print a table of elements; mark visited items with ✓."""
    print(f"\n  {'#':>3}  {'类型':<10}  {'坐标':^12}  标签")
    print(f"  {'-'*3}  {'-'*10}  {'-'*12}  {'-'*24}")
    for i, (ax, ay, label, etype) in enumerate(items):
        mark = "✓" if (visited and i in visited) else " "
        print(f" {mark}{i:>2}  {etype:<10}  ({ax:>5.0f},{ay:>4.0f})  {label}")
