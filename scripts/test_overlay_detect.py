"""
弹窗检测：通过像素差分分析两帧截图是否为弹窗关系。

核心思路：
1. 两帧做绝对差值
2. 分析差值分布 — 大面积低差异(背景不变) + 局部高差异(弹窗区域)
3. 检测背景是否变暗(dimming)
4. 判断是否为弹窗覆盖

用法:
  uv run python tmp_scripts/test_popup_detect.py <before> <after>
  uv run python tmp_scripts/test_popup_detect.py  # 默认用 screen_20260519 帧
"""

import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image


def load_images(path1: str, path2: str):
    img1 = np.array(Image.open(path1).convert("RGB"))
    img2 = np.array(Image.open(path2).convert("RGB"))
    assert img1.shape == img2.shape, f"尺寸不一致: {img1.shape} vs {img2.shape}"
    return img1, img2


def compute_diff(img1, img2):
    return np.abs(img1.astype(np.int16) - img2.astype(np.int16)).astype(np.uint8)


def analyze_brightness_change(img1, img2):
    gray1 = np.mean(img1, axis=2)
    gray2 = np.mean(img2, axis=2)
    diff = gray2 - gray1
    darker_ratio = np.sum(diff < -5) / diff.size
    brighter_ratio = np.sum(diff > 5) / diff.size
    return darker_ratio, brighter_ratio


def find_diff_region(diff_gray, threshold=30):
    binary = (diff_gray > threshold).astype(np.uint8) * 255
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15))
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None, 0, None
    largest = max(contours, key=cv2.contourArea)
    area = cv2.contourArea(largest)
    x, y, w, h = cv2.boundingRect(largest)
    return (x, y, w, h), area, binary


def detect_popup(path1: str, path2: str):
    img1, img2 = load_images(path1, path2)
    h, w = img1.shape[:2]
    total_pixels = h * w

    diff = compute_diff(img1, img2)
    diff_gray = np.max(diff, axis=2)

    mean_diff = np.mean(diff_gray)
    similar_ratio = np.sum(diff_gray < 20) / total_pixels
    changed_ratio = np.sum(diff_gray > 30) / total_pixels

    darker_ratio, brighter_ratio = analyze_brightness_change(img1, img2)

    region, region_area, binary_mask = find_diff_region(diff_gray, threshold=30)
    region_ratio = region_area / total_pixels if region_area > 0 else 0

    # 变化收敛度：真实弹窗的变化应集中在弹窗区域内
    # changed_ratio ≈ region_ratio → 变化收敛
    # changed_ratio >> region_ratio → 变化散乱，不是弹窗
    concentration = region_ratio / changed_ratio if changed_ratio > 0.01 else 0

    # 判断逻辑
    reasons = []
    score = 0
    if similar_ratio > 0.5:
        score += 1
        reasons.append(f"背景相似 {similar_ratio:.1%}")
    if darker_ratio > 0.2:
        score += 1
        reasons.append(f"背景变暗 {darker_ratio:.1%}")
    if region and 0.02 < region_ratio < 0.6:
        score += 1
        reasons.append(f"差异集中 {region_ratio:.1%}")
    if mean_diff < 80:
        score += 1
        reasons.append(f"整体差异小 {mean_diff:.1f}")
    # 硬性门槛：变化必须收敛（散乱变化不是弹窗）
    concentrated = concentration > 0.6
    if not concentrated:
        reasons.append(f"变化散乱 {concentration:.0%} (<60%)")
    else:
        reasons.append(f"变化收敛 {concentration:.0%}")

    is_popup = score >= 3 and concentrated

    return {
        "is_popup": is_popup,
        "score": score,
        "mean_diff": float(mean_diff),
        "similar_ratio": float(similar_ratio),
        "changed_ratio": float(changed_ratio),
        "darker_ratio": float(darker_ratio),
        "brighter_ratio": float(brighter_ratio),
        "region": region,
        "region_ratio": float(region_ratio),
        "concentration": float(concentration),
        "reasons": reasons,
        "img2": img2,
        "diff_gray": diff_gray,
        "binary_mask": binary_mask,
    }


def save_visual(result, out_path: str):
    """生成可视化对比图：原图+弹窗框 | 差值热力图 | 二值掩码"""
    img2 = result["img2"].copy()
    diff_gray = result["diff_gray"]
    binary_mask = result["binary_mask"]

    h, w = img2.shape[:2]

    # --- 左图：弹窗框标注在 after 帧上 ---
    vis_img = img2.copy()
    if result["region"]:
        x, y, rw, rh = result["region"]
        # 绿色粗框标注预测弹窗区域
        cv2.rectangle(vis_img, (x, y), (x + rw, y + rh), (0, 255, 0), 4)
        # 角点标记（让框更醒目）
        corner_len = min(30, rw // 3, rh // 3)
        for cx, cy in [(x, y), (x + rw, y), (x, y + rh), (x + rw, y + rh)]:
            dx = corner_len if cx == x else -corner_len
            dy = corner_len if cy == y else -corner_len
            cv2.line(vis_img, (cx, cy), (cx + dx, cy), (0, 255, 0), 6)
            cv2.line(vis_img, (cx, cy), (cx, cy + dy), (0, 255, 0), 6)
        # 文字标签
        label = f"POPUP {rw}x{rh}" if result["is_popup"] else f"region {rw}x{rh}"
        color = (0, 255, 0) if result["is_popup"] else (0, 0, 255)
        cv2.putText(vis_img, label, (x, y - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)

    # --- 中图：差值热力图 ---
    heat = np.clip(diff_gray.astype(np.float32) * 3, 0, 255).astype(np.uint8)
    heat_color = cv2.applyColorMap(heat, cv2.COLORMAP_JET)

    # --- 右图：二值掩码 ---
    if binary_mask is not None:
        mask_vis = cv2.cvtColor(binary_mask, cv2.COLOR_GRAY2RGB)
    else:
        mask_vis = np.zeros_like(img2)

    # 拼接三图
    gap = np.full((h, 6, 3), 40, dtype=np.uint8)  # 深灰间隔条
    combined = np.concatenate([vis_img, gap, heat_color, gap, mask_vis], axis=1)

    # 顶部加文字条
    bar_h = 50
    bar = np.full((bar_h, combined.shape[1], 3), 30, dtype=np.uint8)
    tag = "POPUP DETECTED" if result["is_popup"] else "NOT POPUP"
    tag_color = (0, 255, 0) if result["is_popup"] else (0, 0, 255)
    cv2.putText(bar, f"{tag}  score={result['score']}/4  diff={result['mean_diff']:.1f}",
                (15, 33), cv2.FONT_HERSHEY_SIMPLEX, 0.9, tag_color, 2)

    # 指标文字
    info = (f"similar={result['similar_ratio']:.1%}  darker={result['darker_ratio']:.1%}  "
            f"changed={result['changed_ratio']:.1%}  region={result['region_ratio']:.1%}  "
            f"concentration={result['concentration']:.0%}")
    cv2.putText(bar, info, (15, combined.shape[1] - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (180, 180, 180), 1)

    final = np.concatenate([bar, combined], axis=0)
    cv2.imwrite(out_path, cv2.cvtColor(final, cv2.COLOR_RGB2BGR))
    print(f"  Visual saved: {out_path}")


def main():
    if len(sys.argv) >= 3:
        p1, p2 = sys.argv[1], sys.argv[2]
    else:
        p1 = "Images/screen_20260519_154110.png"
        p2 = "Images/screen_20260519_154116.png"

    print(f"  Before: {p1}")
    print(f"  After:  {p2}")

    result = detect_popup(p1, p2)

    tag = "✅ 弹窗" if result["is_popup"] else "❌ 非弹窗"
    print(f"\n  判定: {tag} (score={result['score']}/4)")
    print(f"  平均差值: {result['mean_diff']:.1f}")
    print(f"  相似: {result['similar_ratio']:.1%} | 变化: {result['changed_ratio']:.1%}")
    print(f"  变暗: {result['darker_ratio']:.1%} | 变亮: {result['brighter_ratio']:.1%}")
    if result["region"]:
        x, y, w, h = result["region"]
        print(f"  差异区域: ({x},{y}) {w}x{h} 占比={result['region_ratio']:.1%}")
    for r in result["reasons"]:
        print(f"  · {r}")

    out_path = "tmp_scripts/popup_test_out/popup_visual.png"
    save_visual(result, out_path)


if __name__ == "__main__":
    main()
