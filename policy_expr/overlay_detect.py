"""
Detect UI overlays (popup / keyboard) from consecutive screenshots.

Usage:
    result = detect_overlay(img1, img2)
    # result.type  : "popup" | "keyboard" | "none"
    # result.bbox  : (x1, y1, x2, y2) in pixels, or None
"""

from dataclasses import dataclass
from typing import Literal

import cv2
import numpy as np


@dataclass
class OverlayResult:
    type: Literal["popup", "keyboard", "none"]
    bbox: tuple[int, int, int, int] | None  # x1, y1, x2, y2


# ---------------------------------------------------------------------------
# Keyboard detection (single-frame, neutrality-based)
# ---------------------------------------------------------------------------

def _find_keyboard_bbox(img: np.ndarray) -> tuple[int, int, int, int] | None:
    h, w = img.shape[:2]
    results = []
    for pct in range(40, 93):
        y0 = int(h * pct / 100)
        y1 = int(h * (pct + 1) / 100)
        band = img[y0:y1, :]
        mean = band.mean(axis=(0, 1))
        brightness = mean.mean()
        neutrality = 1 - np.std(mean) / (brightness + 1e-6)
        results.append((y0, y1, 200 < brightness < 241 and neutrality > 0.995))

    # 底部锚定：88~92% 至少 3 帯通过
    bottom_hits = sum(1 for i in range(48, 53) if results[i][2])  # pct 88~92 → index 48~52
    if bottom_hits < 3:
        return None

    # 从下往上找最长连续通过段
    run_end = run_len = 0
    best_len = best_top = 0
    for i in range(len(results) - 1, -1, -1):
        y0, y1, ok = results[i]
        if ok:
            if run_end == 0:
                run_end = y1
            run_len += 1
            if run_len > best_len:
                best_len = run_len
                best_top = y0
        else:
            run_end = run_len = 0

    if best_len < 10:
        return None

    return (0, best_top, w, int(h * 0.93))


# ---------------------------------------------------------------------------
# Popup detection (dual-frame, pixel diff based)
# ---------------------------------------------------------------------------

def _find_popup_bbox(
    img1: np.ndarray, img2: np.ndarray
) -> tuple[int, int, int, int] | None:
    h, w = img1.shape[:2]
    total_pixels = h * w

    diff = np.abs(img1.astype(np.int16) - img2.astype(np.int16)).astype(np.uint8)
    diff_gray = np.max(diff, axis=2)

    mean_diff = float(np.mean(diff_gray))
    similar_ratio = float(np.sum(diff_gray < 20) / total_pixels)
    changed_ratio = float(np.sum(diff_gray > 30) / total_pixels)

    # Large-scale change → page navigation, not an overlay appearing
    if changed_ratio > 0.15:
        return None

    gray1 = np.mean(img1, axis=2)
    gray2 = np.mean(img2, axis=2)
    darker_ratio = float(np.sum((gray2 - gray1) < -5) / total_pixels)

    # 找最大连通差异区域
    binary = (diff_gray > 30).astype(np.uint8) * 255
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15))
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    largest = max(contours, key=cv2.contourArea)
    region_area = float(cv2.contourArea(largest))
    region_ratio = region_area / total_pixels
    rx, ry, rw, rh = cv2.boundingRect(largest)
    concentration = region_ratio / changed_ratio if changed_ratio > 0.01 else 0.0

    score = 0
    if similar_ratio > 0.5:
        score += 1
    if darker_ratio > 0.2:
        score += 1
    if 0.02 < region_ratio < 0.6:
        score += 1
    if mean_diff < 80:
        score += 1

    if score < 3 or concentration <= 0.6:
        return None

    return (rx, ry, rx + rw, ry + rh)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_fullscreen_popup(img: np.ndarray, threshold: float = 0.25) -> bool:
    """Single-image heuristic: darkened edge strip vs bright center → fullscreen popup.

    Works where detect_overlay fails (>80% pixels changed looks like navigation).
    """
    h, w = img.shape[:2]
    W = 40
    edge_mean = float(np.mean([
        img[:W].mean(), img[-W:].mean(),
        img[:, :W].mean(), img[:, -W:].mean(),
    ]))
    center_mean = float(img[h // 4:3 * h // 4, w // 4:3 * w // 4].mean())
    ratio = edge_mean / max(center_mean, 1.0)
    return ratio < threshold


def detect_overlay(img1: np.ndarray, img2: np.ndarray) -> OverlayResult:
    """
    img1: before frame (H x W x 3, RGB uint8)
    img2: after frame  (H x W x 3, RGB uint8)
    """
    kbd = _find_keyboard_bbox(img2)
    if kbd:
        return OverlayResult(type="keyboard", bbox=kbd)

    popup = _find_popup_bbox(img1, img2)
    if popup:
        return OverlayResult(type="popup", bbox=popup)

    return OverlayResult(type="none", bbox=None)
