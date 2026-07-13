"""Frame analysis — the low-level VISUAL judgments shared by the runner (settle / effect)
and the milestone supervisor (stuck / blank), kept out of those control-flow modules.

Each function answers ONE narrow pixel question about screenshots:
  - ``frame_diff``     — grayscale mean abs diff. A weak STABILITY signal only ("did the
                         picture stop moving"); NOT a judge of whether an action worked.
  - ``frame_changed``  — did the screen MEANINGFULLY change (action effect)? Structure
                         (SSIM) + color (changed-pixel ratio) + mean, any one over the bar.
  - ``region_change``  — two-tier change (whole-frame grayscale similarity + the SSIM structural
                         change inside the box the action touched); the stuck detector's metric.
  - ``is_blank_screen`` / ``is_loading_frame`` — is this a blank / still-loading page (wait)?

DESIGN NOTE: a grayscale mean is the wrong judge of "did the action work" or "is the task
done" — it dilutes localized change, is blind to color (a tab going gray→blue), and is
polluted by dynamic regions. Use ``frame_changed`` for effect, the LLM checker for completion.

Leaf module: imports only PIL / numpy / skimage and ``core.schemas`` (no runner/policy), so
there is no import cycle. numpy / skimage are imported lazily inside functions to keep
import-time light.
"""

from __future__ import annotations

import io
from typing import Optional

from PIL import Image, ImageStat

from gui_agent.core.schemas import Observation

# ── thresholds ─────────────────────────────────────────────────────────────
# frame_changed (动作是否生效): structure + color signals, ANY one over its bar => changed.
CHANGE_MEAN_THR = 8.0           # 灰度均值差佐证信号(噪声地板 ~0.05；冻结/静止邻帧 <1.1)
CHANGE_SSIM_DIST_THR = 0.08     # 1-SSIM 结构差(主信号)：tab 切换 0.167 vs 静止邻帧 ≤0.03
CHANGED_PIXEL_THR = 0.025       # 变色像素占比(任一通道差 >25)：tab 切换 0.042 vs 静止 ≤0.013
EFFECT_REGION_HALF = 0.12       # tap 生效检测：点击点周围 2D box 半边(占比，24% box)——局部 UI
                                # 改动(菜单展开/下拉/勾选)整帧会被稀释，裁到点击点附近用 SSIM+颜色
                                # 判：实测菜单展开局部 ssim_dist≈0.29 / changed_ratio≈0.21 ≫ 阈值
# frame_diff (画面是否停稳) — stability only, grayscale is fine here.
STABLE_MEAN_THR = 2.0           # 相邻帧灰度均值差低于此即视为画面已停稳
# region_change (两级 stuck 度量) defaults。局部级 = 1-SSIM(结构),阈值复用 CHANGE_SSIM_DIST_THR。
SIM_GRID = 256                  # 分析用灰度边长
LOCAL_REGION_HALF = 0.14        # 动作坐标周围方框半边(占比，28% box)
# is_blank_screen: a blank/loading body is a large, LIGHT, near-UNIFORM region.
BLANK_BODY_MEAN_MIN = 225.0     # body must be light (rules out dark splash/dark mode)
BLANK_BODY_STD_MAX = 12.0       # body must be near-uniform (text/icons push std up)


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


def frame_changed(
    png_a: bytes,
    png_b: bytes,
    focus_y: float | None = None,
    center: tuple[float, float] | None = None,
    box_half: float = EFFECT_REGION_HALF,
) -> bool:
    """两帧之间「屏幕是否发生了有意义的变化」——动作生效判定专用。

    不用全屏灰度均值差当裁判：它会稀释局部变化(卡片/底部导航)、对颜色变化(tab 灰→蓝)盲、
    且被秒针/状态栏/动画等动态区污染。改从**结构**(SSIM)+**颜色**(变色像素占比)信号判，并
    **偏向"变了"**：SSIM 距离、变色像素占比、灰度均值差三者**任一**过线即 True。调用方仅当本
    判定在所有 settle 轮里都为 False(三信号全弱)才置 no_effect——而且那也只是个提示，最终
    是否完成由语义层 checker 裁决。

    聚焦(都对**同一套 SSIM+颜色信号**生效，只是缩小比较范围让局部改动凸显，绝不退回灰度)：
      - ``center``(tap 的归一化 0-1000 x/y)：只看点击点周围 ``box_half`` 的 2D box。tap 触发的
        是**局部** UI 改动(菜单展开/下拉/勾选/边栏)，整帧 SSIM 会被稀释成「没变」(实测菜单展开
        整帧 ssim_dist 仅 0.026<0.08，而点击点 24% box 内达 0.29)——裁到点击点附近才看得见。
      - ``focus_y``(归一化 0-1000)：只看该 y 周围横向带(type 只改输入框那一行)。
      - 都不给：整帧。``center`` 优先于 ``focus_y``。
    """
    import numpy as np
    from skimage.metrics import structural_similarity as ssim

    a = np.asarray(Image.open(io.BytesIO(png_a)).convert("RGB").resize((160, 320)), dtype=np.int16)
    b = np.asarray(Image.open(io.BytesIO(png_b)).convert("RGB").resize((160, 320)), dtype=np.int16)
    if center is not None:
        cx, cy = center[0] / 1000.0 * 160.0, center[1] / 1000.0 * 320.0
        hx, hy = box_half * 160.0, box_half * 320.0
        x0, x1 = max(0, int(cx - hx)), min(160, int(cx + hx))
        y0, y1 = max(0, int(cy - hy)), min(320, int(cy + hy))
        if (x1 - x0) >= 8 and (y1 - y0) >= 8:  # SSIM 窗口下限；太小退回整帧
            a, b = a[y0:y1, x0:x1], b[y0:y1, x0:x1]
    elif focus_y is not None:
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


def region_change(
    png1: bytes,
    png2: bytes,
    center: Optional[tuple[float, float]] = None,
    size: int = SIM_GRID,
    half: float = LOCAL_REGION_HALF,
) -> tuple[float, Optional[float]]:
    """Two-tier change between two frames.

    Returns ``(global_sim, local_change)``:
      - ``global_sim``  = 1 - grayscale mean abs diff over the whole frame. The 全局 tier asks
        「整帧还是不是原样」(a sameness/STABILITY question — grayscale is fine for that).
      - ``local_change``= ``1 - SSIM`` (structural distance) inside a box of half-size ``half``
        around ``center`` (the action's normalized 0-1000 x/y). The 局部 tier asks「动作所在区域
        动没动」(a CHANGE question → SSIM, not grayscale — consistent with ``frame_changed``;
        avoids the grayscale failure mode the module DESIGN NOTE warns about). A picker step
        keeps global_sim ~0.999 yet drives local_change to ~0.24; a no-op leaves it ~0.0.
        Threshold against ``CHANGE_SSIM_DIST_THR``. ``None`` when the action had no coordinates
        (press_enter / home / back) — the caller falls back to the global tier alone.
    """
    import numpy as np
    from skimage.metrics import structural_similarity as ssim

    a = np.asarray(Image.open(io.BytesIO(png1)).convert("L").resize((size, size)), dtype=np.float64)
    b = np.asarray(Image.open(io.BytesIO(png2)).convert("L").resize((size, size)), dtype=np.float64)
    global_sim = 1.0 - float(np.abs(a - b).mean()) / 255.0
    if center is None:
        return global_sim, None
    cx, cy = center[0] / 1000 * size, center[1] / 1000 * size
    h = half * size
    x0, x1 = max(0, int(cx - h)), min(size, int(cx + h))
    y0, y1 = max(0, int(cy - h)), min(size, int(cy + h))
    if x1 - x0 < 7 or y1 - y0 < 7:  # SSIM 窗口下限(默认 7×7)；太小退回整帧
        return global_sim, None
    local_change = 1.0 - float(ssim(a[y0:y1, x0:x1], b[y0:y1, x0:x1], data_range=255))
    return global_sim, local_change


def is_blank_screen(png_bytes: bytes) -> bool:
    """Return True if the screenshot is a blank/loading screen (content not yet rendered).

    A blank/loading body is a large, LIGHT, near-UNIFORM region. We measure the app content
    body, not the full frame, because the iPhone Mirroring black bezel dilutes any whole-image
    ratio, and this app's blank body renders ~gray 239 (not pure white). So: trim the black
    bezel (bbox of non-dark pixels), drop the top chrome (status bar / header / toolbar), then
    treat the body as blank when it is both light (mean ≥ BLANK_BODY_MEAN_MIN) and near-uniform
    (stddev ≤ BLANK_BODY_STD_MAX). Text, icons or a rendered list push the stddev up.
    """
    img = Image.open(io.BytesIO(png_bytes)).convert("L")
    bbox = img.point(lambda p: 255 if p > 40 else 0).getbbox()  # trim the black bezel
    if bbox:
        img = img.crop(bbox)
    w, h = img.size
    if w == 0 or h == 0:
        return False
    body = img.crop((0, int(h * 0.18), w, int(h * 0.97)))  # below top chrome, above home indicator
    if body.width == 0 or body.height == 0:
        return False
    stat = ImageStat.Stat(body)
    mean, std = stat.mean[0], stat.stddev[0]
    return mean >= BLANK_BODY_MEAN_MIN and std <= BLANK_BODY_STD_MAX


def is_loading_frame(observation: Observation) -> bool:
    """Whether this frame is an unstable / still-loading page (so wait, don't act).

    Prefer the PLATFORM's structural signal when perception supplies one
    (``observation.loading`` — e.g. browser's ``document.readyState != 'complete'``). The
    iphone PIXEL heuristic (``is_blank_screen``: large light uniform body) is used ONLY as the
    fallback when the platform gives no signal (``loading is None``), because on desktop web it
    MISFIRES — a rendered-but-sparse page (an empty doc editor, a wide centered layout) is
    mostly whitespace and reads as 'blank'.
    """
    if observation.loading is not None:
        return observation.loading
    return is_blank_screen(observation.png_bytes)
