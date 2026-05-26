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

    def nearest(self, target_x: float, target_y: float, max_dist: float = 30.0) -> tuple[float, float] | None:
        """Find the detected icon nearest to (target_x, target_y) within max_dist.

        When two boxes are within 30px of each other (distance-wise), prefer
        the one with higher confidence to avoid low-confidence misdetections
        pulling the snap away from the correct target.
        """
        candidates: list[tuple[float, tuple[float, float], float]] = []
        for b in self.boxes:
            nx = b.cx / self.img_w * 1000
            ny = b.cy / self.img_h * 1000
            dist = ((nx - target_x) ** 2 + (ny - target_y) ** 2) ** 0.5
            if dist <= max_dist:
                candidates.append((dist, (nx, ny), b.conf))
        if not candidates:
            return None
        candidates.sort(key=lambda c: c[0])
        best_dist = candidates[0][0]
        # Among candidates within 30px of the closest, pick highest confidence
        tied = [c for c in candidates if c[0] <= best_dist + 30]
        tied.sort(key=lambda c: c[2], reverse=True)
        return tied[0][1]
