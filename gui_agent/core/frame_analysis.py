"""Frame analysis — low-level VISUAL judgments about screenshots, kept out of the runner's
control flow.

  - ``frame_diff``    — grayscale mean abs diff. A weak STABILITY signal only ("did the
                        picture stop moving"); NOT a judge of whether an action worked.
  - ``frame_changed`` — did the screen MEANINGFULLY change (action effect)? Structure
                        (SSIM) + color (changed-pixel ratio) + mean, any one over the bar.

DESIGN NOTE: a grayscale mean is the wrong judge of "did the action work" — it dilutes
localized change, is blind to color (a tab going gray→blue), and is polluted by dynamic
regions. Use ``frame_changed`` for effect; the LLM checker for completion.

Leaf module: imports only PIL / numpy / skimage. numpy / skimage are imported lazily inside
functions to keep import-time light.
"""

from __future__ import annotations

import io

from PIL import Image

# ── thresholds ─────────────────────────────────────────────────────────────
# frame_changed (动作是否生效): structure + color signals, ANY one over its bar => changed.
CHANGE_MEAN_THR = 8.0           # 灰度均值差佐证信号(噪声地板 ~0.05；冻结/静止邻帧 <1.1)
CHANGE_SSIM_DIST_THR = 0.08     # 1-SSIM 结构差(主信号)：tab 切换 0.167 vs 静止邻帧 ≤0.03
CHANGED_PIXEL_THR = 0.025       # 变色像素占比(任一通道差 >25)：tab 切换 0.042 vs 静止 ≤0.013
# frame_diff (画面是否停稳) — stability only, grayscale is fine here.
STABLE_MEAN_THR = 2.0           # 相邻帧灰度均值差低于此即视为画面已停稳


def frame_diff(png_a: bytes, png_b: bytes, focus_y: float | None = None) -> float:
    """两帧灰度图缩放到 160x320 后的平均绝对差（0-255 量级）。**仅作低层稳定性信号**
    （判相邻帧是否停稳），不要拿它判「动作是否生效」——用 ``frame_changed``。

    focus_y（归一化 0-1000）给定时只比该 y 周围的横向带，而非整帧——type 是**局部改动**
    （只改输入框那一行），整帧均值会把它稀释；裁到输入行带后局部改动凸显。点击/跳转等
    影响整页的动作不传 focus_y，仍按整帧比。
    """
    import numpy as np

    a = np.array(Image.open(io.BytesIO(png_a)).convert("L").resize((160, 320)), dtype=np.float32)
    b = np.array(Image.open(io.BytesIO(png_b)).convert("L").resize((160, 320)), dtype=np.float32)
    if focus_y is not None:
        cy = focus_y / 1000.0 * 320.0
        half = 0.05 * 320.0  # 输入行带半高 ~16px
        y0, y1 = max(0, int(cy - half)), min(320, int(cy + half))
        if y1 - y0 >= 4:  # 防退化裁剪
            a, b = a[y0:y1], b[y0:y1]
    return float(np.abs(a - b).mean())


def frame_changed(png_a: bytes, png_b: bytes, focus_y: float | None = None) -> bool:
    """两帧之间「屏幕是否发生了有意义的变化」——动作生效判定专用。

    不用全屏灰度均值差当裁判：它会稀释局部变化(卡片/底部导航)、对颜色变化(tab 灰→蓝)盲、
    且被秒针/状态栏/动画等动态区污染。改从**结构**(SSIM)+**颜色**(变色像素占比)信号判，并
    **偏向"变了"**：SSIM 距离、变色像素占比、灰度均值差三者**任一**过线即 True。调用方仅当本
    判定在所有 settle 轮里都为 False(三信号全弱)才置 no_effect——而且那也只是个提示，最终
    是否完成由语义层 checker 裁决。

    ``focus_y``(归一化 0-1000)给定时只看该 y 周围横向带(type 是局部改动，整帧会稀释)。
    """
    import numpy as np
    from skimage.metrics import structural_similarity as ssim

    a = np.asarray(Image.open(io.BytesIO(png_a)).convert("RGB").resize((160, 320)), dtype=np.int16)
    b = np.asarray(Image.open(io.BytesIO(png_b)).convert("RGB").resize((160, 320)), dtype=np.int16)
    if focus_y is not None:
        cy = focus_y / 1000.0 * 320.0
        half = 0.05 * 320.0
        y0, y1 = max(0, int(cy - half)), min(320, int(cy + half))
        if y1 - y0 >= 8:  # SSIM 窗口下限；太薄则退回整帧
            a, b = a[y0:y1], b[y0:y1]
    ga, gb = a.mean(axis=2), b.mean(axis=2)  # 灰度
    mean_diff = float(np.abs(ga - gb).mean())
    ssim_dist = 1.0 - float(ssim(ga, gb, data_range=255))
    changed_ratio = float((np.abs(a - b).max(axis=2) > 25).mean())  # 任一通道变化 >25 的像素占比
    return (
        ssim_dist > CHANGE_SSIM_DIST_THR
        or changed_ratio > CHANGED_PIXEL_THR
        or mean_diff > CHANGE_MEAN_THR
    )
