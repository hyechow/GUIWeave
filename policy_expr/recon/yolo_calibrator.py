"""YOLO calibrator: detect icons once, find nearest to any target point."""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image

import numpy as np

from policy_expr.recon.icon_detector import IconBbox, IconDetector


def _content_top(img: Image.Image, threshold: int = 30) -> int:
    """Return the y-coordinate where the SCK black border ends."""
    arr = np.array(img.convert("L"))
    row_max = arr.max(axis=1)
    above = np.where(row_max > threshold)[0]
    return int(above[0]) if len(above) > 0 else 0


@dataclass
class YoloCalibrator:
    """Run YOLO detection once, then query nearest icon to any target.

    All coordinates are in normalized 0-1000 space.
    """
    boxes: list[IconBbox]
    img_w: int
    img_h: int

    @classmethod
    def from_png(cls, png_bytes: bytes, conf: float = 0.1) -> YoloCalibrator | None:
        """Create calibrator from screenshot. Returns None if no icons detected."""
        det = IconDetector(conf=conf)
        boxes = det.detect(png_bytes)
        if not boxes:
            return None
        img = Image.open(io.BytesIO(png_bytes))
        # Filter boxes in the SCK black border + iOS status bar area.
        # Black border (notch surround) is detected dynamically;
        # iOS status bar (~47pt) is added on top to exclude time/signal icons.
        content_top = _content_top(img)
        status_bar_h = int(47 * img.height / 844)
        min_cy = content_top + status_bar_h
        boxes = [b for b in boxes if b.cy >= min_cy]
        if not boxes:
            return None
        return cls(boxes, img.width, img.height)

    # A box must be at least this confident before its bbox is trusted to
    # *contain* the target (Tier 1). YOLO runs at conf=0.1 to catch every icon,
    # so weak boxes are common and a single low-conf misdetection can sprawl
    # across half the screen — trusting its containment would hijack the snap.
    # Below this floor a box still competes by center distance (Tiers 2/3),
    # which a misdetection's center rarely wins.
    _REAL_CONTAINMENT_MIN_CONF = 0.4

    # Snapping is a nudge, not a teleport: never move the tap farther than this
    # from the model's point. A full-width header/banner box can "contain" a
    # top-left back-arrow tap yet have its center ~400 units away mid-screen —
    # snapping there is far worse than not snapping. Boxes whose center exceeds
    # this are dropped so a closer (e.g. margin-tier) candidate can win instead.
    _MAX_SNAP_MOVE = 150.0

    def nearest(
        self,
        target_x: float,
        target_y: float,
        max_dist: float = 30.0,
        bbox_margin: float = 50.0,
    ) -> tuple[float, float] | None:
        """Find the detected icon to snap (target_x, target_y) to.

        Tiered so a small icon near the point can't steal a tap that actually
        lands inside a larger element:
          1. Point inside a box's real bbox (and conf ≥ floor) → snap to the
             tightest such box (the most specific element under the point).
          2. Point within max_dist of a box center → nearest center wins.
          3. Point inside a bbox expanded by bbox_margin → nearest center wins
             (rescues coordinates the model estimated just outside an element).
        Confidence breaks ties so low-confidence misdetections don't win, and a
        candidate whose center is farther than _MAX_SNAP_MOVE is never used.
        """
        real_hits: list[tuple[float, float, tuple[float, float], float]] = []
        near_hits: list[tuple[float, tuple[float, float], float]] = []
        margin_hits: list[tuple[float, tuple[float, float], float]] = []
        for b in self.boxes:
            nx = b.cx / self.img_w * 1000
            ny = b.cy / self.img_h * 1000
            x1 = b.x1 / self.img_w * 1000
            y1 = b.y1 / self.img_h * 1000
            x2 = b.x2 / self.img_w * 1000
            y2 = b.y2 / self.img_h * 1000
            dist = ((nx - target_x) ** 2 + (ny - target_y) ** 2) ** 0.5
            center = (nx, ny)
            if dist > self._MAX_SNAP_MOVE:
                continue  # too far to be a sane snap target, whatever the tier
            if (
                x1 <= target_x <= x2 and y1 <= target_y <= y2
                and b.conf >= self._REAL_CONTAINMENT_MIN_CONF
            ):
                area = (x2 - x1) * (y2 - y1)
                real_hits.append((area, dist, center, b.conf))
            if dist <= max_dist:
                near_hits.append((dist, center, b.conf))
            if (
                x1 - bbox_margin <= target_x <= x2 + bbox_margin
                and y1 - bbox_margin <= target_y <= y2 + bbox_margin
            ):
                margin_hits.append((dist, center, b.conf))

        if real_hits:
            # Tightest box = most specific element; tie by distance then conf.
            real_hits.sort(key=lambda c: (c[0], c[1], -c[3]))
            return real_hits[0][2]
        if near_hits:
            near_hits.sort(key=lambda c: c[0])
            best_dist = near_hits[0][0]
            # Among centers within 30px of the closest, pick highest confidence.
            tied = [c for c in near_hits if c[0] <= best_dist + 30]
            tied.sort(key=lambda c: c[2], reverse=True)
            return tied[0][1]
        if margin_hits:
            margin_hits.sort(key=lambda c: (c[0], -c[2]))
            return margin_hits[0][1]
        return None

    # Locking OCR out (treating the point as a deliberate icon hit) is a strong
    # move, so it needs more confidence than mere Tier-1 containment: a weak box
    # (e.g. conf 0.40) that happens to sit under a too-high tab estimate must NOT
    # suppress an OCR match on the real tab label below it.
    _OCR_LOCK_MIN_CONF = 0.45

    def contains(self, target_x: float, target_y: float) -> bool:
        """Whether the point falls inside a confidently-detected icon box.

        Used to tell a deliberate icon target (LLM aimed inside a real icon)
        from a point that merely sits near some text — so OCR doesn't drag an
        icon tap onto an adjacent label. Uses a higher floor than Tier-1
        containment because suppressing OCR is a strong action.
        """
        for b in self.boxes:
            if b.conf < self._OCR_LOCK_MIN_CONF:
                continue
            x1 = b.x1 / self.img_w * 1000
            y1 = b.y1 / self.img_h * 1000
            x2 = b.x2 / self.img_w * 1000
            y2 = b.y2 / self.img_h * 1000
            if x1 <= target_x <= x2 and y1 <= target_y <= y2:
                return True
        return False
