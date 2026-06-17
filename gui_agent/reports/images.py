"""Image annotation helpers for HTML reports."""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# ── Action types & colors ──────────────────────────────────────

ACTION_COLORS: dict[str, tuple[int, int, int]] = {
    "tap": (220, 50, 50),
    "home": (50, 120, 220),
    "swipe": (50, 180, 50),
    "text": (160, 50, 200),
    "back": (220, 160, 0),
    "none": (128, 128, 128),
}

DEFAULT_COLOR = ACTION_COLORS["tap"]

COLOR_NAVIGATED = (34, 197, 94)    # green
COLOR_NO_CHANGE = (156, 163, 175)  # gray

DOT_RADIUS = 14
FONT_SIZE = 16


def _font(size: int = FONT_SIZE):
    try:
        return ImageFont.truetype("/System/Library/Fonts/PingFang.ttc", size)
    except Exception:
        return ImageFont.load_default()


_REPORT_MAX_W = 640  # resize annotated images to this width before saving


def _save_report_img(src: "Image.Image | bytes", path: Path, quality: int = 75, max_w: int | None = _REPORT_MAX_W) -> None:
    """Save an image as JPEG to disk, resizing to ``max_w`` if wider. ``max_w=None`` keeps full
    resolution (used for zoom-grade annotated frames: the thumbnail stays downscaled, but the
    click-to-zoom large image keeps BOTH full resolution AND the action annotation)."""
    if isinstance(src, bytes):
        img = Image.open(io.BytesIO(src)).convert("RGB")
    else:
        img = src.convert("RGB")
    w, h = img.size
    if max_w and w > max_w:
        img = img.resize((max_w, round(h * max_w / w)), Image.Resampling.LANCZOS)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="JPEG", quality=quality, optimize=True)


def _load_img(path: Path) -> Image.Image:
    return Image.open(path).convert("RGBA")



def annotate_tap(
    img: Image.Image,
    points: list[tuple[float, float, int]],
) -> Image.Image:
    """Draw numbered tap points on image. points: (x_pct, y_pct, index)."""
    img = img.copy()
    draw = ImageDraw.Draw(img, "RGBA")
    w, h = img.size
    font = _font(FONT_SIZE)

    for x_pct, y_pct, idx in points:
        cx = int(x_pct / 1000 * w)
        cy = int(y_pct / 1000 * h)
        color = DEFAULT_COLOR
        draw.ellipse(
            [cx - DOT_RADIUS, cy - DOT_RADIUS, cx + DOT_RADIUS, cy + DOT_RADIUS],
            fill=(*color, 200),
            outline=(255, 255, 255, 255),
            width=2,
        )
        text = str(idx)
        bbox = font.getbbox(text)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text((cx - tw // 2, cy - th // 2), text, fill=(255, 255, 255, 255), font=font)

    return img


_ACTION_COLORS_FULL: dict[str, tuple[int, int, int]] = {
    "tap": (220, 50, 50),
    "type": (160, 50, 200),
    "scroll": (50, 180, 50),
    "drag": (50, 150, 220),
    "home": (50, 120, 220),
    "press_enter": (220, 160, 0),
    "clear_text": (128, 128, 128),
    "upload": (236, 72, 153),  # pink — distinct from tap(red)/type(violet) on the annotation
}


def annotate_action(
    img: Image.Image,
    action_type: str,
    x: float | None,
    y: float | None,
    idx: int,
    direction: str | None = None,
    text: str | None = None,
    to_x: float | None = None,
    to_y: float | None = None,
    snap: dict | None = None,
) -> Image.Image:
    """Draw action annotation on image with type-specific visuals."""
    img = img.copy()
    draw = ImageDraw.Draw(img, "RGBA")
    w, h = img.size
    font = _font(FONT_SIZE)
    small_font = _font(12)
    color = _ACTION_COLORS_FULL.get(action_type, DEFAULT_COLOR)

    if x is None or y is None:
        return img
    # When snap exists, draw main marker at snapped position (where action actually executed)
    if snap and snap.get("snapped"):
        cx = int(snap["snapped"][0] / 1000 * w)
        cy = int(snap["snapped"][1] / 1000 * h)
    else:
        cx = int(x / 1000 * w)
        cy = int(y / 1000 * h)

    if action_type in ("scroll", "drag"):
        # Draw start circle + arrow to end
        draw.ellipse(
            [cx - DOT_RADIUS, cy - DOT_RADIUS, cx + DOT_RADIUS, cy + DOT_RADIUS],
            fill=(*color, 200),
            outline=(255, 255, 255, 255),
            width=2,
        )
        # Direction arrow or end point
        if to_x is not None and to_y is not None:
            ex = int(to_x / 1000 * w)
            ey = int(to_y / 1000 * h)
            draw.line([(cx, cy), (ex, ey)], fill=(*color, 220), width=3)
            draw.ellipse(
                [ex - 8, ey - 8, ex + 8, ey + 8],
                fill=(*color, 180),
                outline=(255, 255, 255, 255),
                width=2,
            )
        elif direction:
            arrow_len = 60
            dx, dy = 0, 0
            if direction == "up": dy = -arrow_len
            elif direction == "down": dy = arrow_len
            elif direction == "left": dx = -arrow_len
            elif direction == "right": dx = arrow_len
            ex, ey = cx + dx, cy + dy
            draw.line([(cx, cy), (ex, ey)], fill=(*color, 220), width=3)
            # Arrowhead
            import math
            angle = math.atan2(dy, dx)
            a1 = angle + 2.5
            a2 = angle - 2.5
            for a in (a1, a2):
                ax = ex - int(12 * math.cos(a))
                ay = ey - int(12 * math.sin(a))
                draw.line([(ex, ey), (ax, ay)], fill=(*color, 220), width=2)
        # Label
        label = str(idx)
        bbox = font.getbbox(label)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text((cx - tw // 2, cy - th // 2), label, fill=(255, 255, 255, 255), font=font)

    elif action_type == "type":
        # Circle + text bubble
        draw.ellipse(
            [cx - DOT_RADIUS, cy - DOT_RADIUS, cx + DOT_RADIUS, cy + DOT_RADIUS],
            fill=(*color, 200),
            outline=(255, 255, 255, 255),
            width=2,
        )
        label = str(idx)
        bbox = font.getbbox(label)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text((cx - tw // 2, cy - th // 2), label, fill=(255, 255, 255, 255), font=font)
        # Text bubble above
        if text:
            display = text[:20] + ("…" if len(text) > 20 else "")
            tbbox = small_font.getbbox(display)
            btw, bth = tbbox[2] - tbbox[0], tbbox[3] - tbbox[1]
            pad = 4
            bx = cx - btw // 2 - pad
            by = cy - DOT_RADIUS - bth - 14
            draw.rounded_rectangle(
                [bx, by, bx + btw + pad * 2, by + bth + pad * 2],
                radius=4, fill=(255, 255, 255, 230), outline=(*color, 200), width=1,
            )
            draw.text((bx + pad, by + pad), display, fill=(*color, 255), font=small_font)

    else:
        # Default: circle with index (tap, home, press_enter, etc.)
        draw.ellipse(
            [cx - DOT_RADIUS, cy - DOT_RADIUS, cx + DOT_RADIUS, cy + DOT_RADIUS],
            fill=(*color, 200),
            outline=(255, 255, 255, 255),
            width=2,
        )
        label = str(idx)
        if action_type == "press_enter":
            label = "↵"
        elif action_type == "home":
            label = "⌂"
        bbox = font.getbbox(label)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text((cx - tw // 2, cy - th // 2), label, fill=(255, 255, 255, 255), font=font)

    # Draw snap visualization: original (LLM) vs snapped (YOLO/OCR/DOM)
    if snap and snap.get("original"):
        method = snap.get("method", "?").lower()
        # orange=YOLO, cyan=DOM(browser), green=OCR
        snap_color = (
            (34, 211, 238, 220) if method == "dom"
            else (255, 165, 0, 220) if method == "yolo"
            else (0, 200, 100, 220)
        )
        ox = int(snap["original"][0] / 1000 * w)
        oy = int(snap["original"][1] / 1000 * h)
        # Solid circle at original (LLM-predicted) position
        draw.ellipse(
            [ox - DOT_RADIUS, oy - DOT_RADIUS, ox + DOT_RADIUS, oy + DOT_RADIUS],
            outline=snap_color, width=3,
        )
        # Line connecting original to snapped
        draw.line([(ox, oy), (cx, cy)], fill=snap_color, width=2)

    return img


def annotate_recon_taps(
    img: Image.Image,
    points: list[tuple[float, float, int, bool]],
) -> Image.Image:
    """Draw recon tap points: green=navigated, gray=no change. points: (x_pct, y_pct, index, navigated)."""
    img = img.copy()
    draw = ImageDraw.Draw(img, "RGBA")
    w, h = img.size
    font = _font(FONT_SIZE)

    for x_pct, y_pct, idx, navigated in points:
        cx = int(x_pct / 1000 * w)
        cy = int(y_pct / 1000 * h)
        color = COLOR_NAVIGATED if navigated else COLOR_NO_CHANGE
        draw.ellipse(
            [cx - DOT_RADIUS, cy - DOT_RADIUS, cx + DOT_RADIUS, cy + DOT_RADIUS],
            fill=(*color, 210),
            outline=(255, 255, 255, 255),
            width=2,
        )
        text = str(idx)
        bbox = font.getbbox(text)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text((cx - tw // 2, cy - th // 2), text, fill=(255, 255, 255, 255), font=font)

    return img


LOGICAL_W = 318  # iPhone Mirroring logical pixel width (matches executor.WIN_W)

STRATEGY_COLORS: dict[str, tuple[int, int, int]] = {
    "fixed": (99, 102, 241),   # indigo
    "YOLO":    (234, 179,  8),   # amber
    "LLM":     (59, 130, 246),   # blue
    "LLM+YOLO":(139,  92, 246),  # violet
}


def annotate_back_attempts_img(
    img: Image.Image,
    attempts: list[dict],
) -> Image.Image:
    """Draw numbered back-attempt circles: green=success, red=fail, by strategy color."""
    img = img.copy()
    draw = ImageDraw.Draw(img, "RGBA")
    w, _ = img.size
    scale = w / LOGICAL_W
    font = _font(13)

    for i, a in enumerate(attempts, 1):
        coords = a.get("coords", [])
        if len(coords) < 2:
            continue
        cx = int(coords[0] * scale)
        cy = int(coords[1] * scale)
        success = a.get("success", False)
        strategy = a.get("strategy", "")
        color = (34, 197, 94) if success else STRATEGY_COLORS.get(strategy, (239, 68, 68))
        draw.ellipse(
            [cx - DOT_RADIUS, cy - DOT_RADIUS, cx + DOT_RADIUS, cy + DOT_RADIUS],
            fill=(*color, 220),
            outline=(255, 255, 255, 255),
            width=2,
        )
        text = str(i)
        bbox = font.getbbox(text)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text((cx - tw // 2, cy - th // 2), text, fill=(255, 255, 255, 255), font=font)

    return img


def img_to_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


# ── JSON renderer ──────────────────────────────────────────
