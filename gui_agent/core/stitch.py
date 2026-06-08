"""Visual frame stitching for scroll collection (platform-neutral).

把连续滚动帧用「鲁棒垂直位移估计」在像素层拼成长内容：每帧只取新露出的条带追加，
重叠区只保留一份 —— 几何去重，不依赖 reader 文本一致性、不需 per-app 标定。
累积到约一屏内容才切出一个 chunk 交给 reader 读，chunk 间保留少量 overlap 防止行被切断。

位移用「图像关键点匹配」估计（ORB），而非全局像素相关/残差：后者在「重复行 + 大片白底」
的列表上会被行混叠 + 白对白主导骗到 shift=0。关键点匹配的是局部独特结构（文字/图标角点），
配合：① 内容带 mask（不在 app 状态栏/底部 chrome 上检测）；② Lowe ratio test + 仅取近乎
纯垂直(|dx|小)的匹配；③ 排除「固定栏」产生的 |dy|≈0 簇，再取剩余主导非零簇。

平台中性化：iPhone 的设备框 mask 与内容带不再写死，而是 PARAMETERS —— iphone adapter 经
``adapters/iphone/stitch.py`` 注入其设备框 mask 与历史内容带(0.10/0.97)，browser adapter 传
``frame_mask=None`` + 全内容带(0.0/1.0，整屏皆内容、无设备框)。纯图像运算，无平台 I/O。
默认值保留 iphone 历史标定，故不带参数的 standalone ``robust_shift`` 行为与搬移前完全一致。
"""

from __future__ import annotations

import io

import cv2
import numpy as np
from PIL import Image

# 默认内容带 + 阈值：保留搬移前 iphone 的历史标定（不传参的 robust_shift 行为不变）。
CONTENT_TOP = 0.10       # 内容带上沿（排 app 顶部状态栏）
CONTENT_BOT = 0.97       # 内容带下沿（排底部 chrome）
MIN_PROGRESS = 14        # |位移| < 此值视为没推进（失败/无效滚动），不追加
_ORB_FEATURES = 2000     # ORB 特征点上限
_RATIO = 0.8             # Lowe ratio test 阈值
_DX_TOL = 8              # 纯垂直：水平漂移 < 此值的匹配才采纳
_CLUSTER_TOL = 8         # 主导簇半宽（px）
_MIN_INLIERS = 15        # 主导非零簇内点数下限，否则判没滚动

# shift==0 有两种：① 同一帧(真没滚)→ 不追加；② 换页/加载跳变(两帧不相干、无可追踪重叠)
# → 不能丢，要重定基线。robust_shift 对两者都返回 0，靠内容带粗相似度区分：低于此阈值=换页。
REBASELINE_SIM = 0.90


def _gray_u8(png_bytes: bytes) -> np.ndarray:
    return np.asarray(Image.open(io.BytesIO(png_bytes)).convert("L"), dtype=np.uint8)


def _content_sim(
    prev_u8: np.ndarray, cur_u8: np.ndarray,
    *, content_top: float = CONTENT_TOP, content_bot: float = CONTENT_BOT,
) -> float:
    """内容带的粗相似度(0~1)：1=完全相同。用于区分『同一帧』与『换页/跳变』。"""
    h = prev_u8.shape[0]
    top, bot = int(h * content_top), int(h * content_bot)
    a = cv2.resize(prev_u8[top:bot], (48, 96)).astype(np.float32)
    b = cv2.resize(cur_u8[top:bot], (48, 96)).astype(np.float32)
    return 1.0 - float(np.abs(a - b).mean()) / 255.0


def _orb_mask(
    h: int, w: int,
    *, content_top: float = CONTENT_TOP, content_bot: float = CONTENT_BOT,
    frame_mask: np.ndarray | None = None,
) -> np.ndarray:
    """ORB 特征检测掩码（uint8 0/255）：内容带 ∩（可选）设备屏幕区。

    ``frame_mask``（与帧同尺寸、设备框处≥128）给定时把设备框排除在特征检测外（iphone）；
    None 或尺寸不符时只用内容带（browser 整屏皆内容）。
    """
    m = np.zeros((h, w), dtype=np.uint8)
    m[int(h * content_top):int(h * content_bot), :] = 255
    if frame_mask is not None and frame_mask.shape == (h, w):
        m[frame_mask >= 128] = 0          # 设备框不检测
    return m


def robust_shift(
    prev_u8: np.ndarray, cur_u8: np.ndarray,
    *, content_top: float = CONTENT_TOP, content_bot: float = CONTENT_BOT,
    frame_mask: np.ndarray | None = None,
) -> tuple[int, float]:
    """关键点匹配估计垂直位移。返回 (shift_px<=0 向下滚, quality 0~1)。

    shift<0 表示内容上移（向下滚动），其绝对值即新露出的底部条带高度；shift==0=没可信推进。
    quality = 主导簇内点占非零匹配的比例（越高越可信）。
    """
    h, w = prev_u8.shape
    mask = _orb_mask(h, w, content_top=content_top, content_bot=content_bot, frame_mask=frame_mask)
    orb = cv2.ORB_create(nfeatures=_ORB_FEATURES)
    ka, da = orb.detectAndCompute(prev_u8, mask)
    kb, db = orb.detectAndCompute(cur_u8, mask)
    if da is None or db is None or len(ka) < _MIN_INLIERS or len(kb) < _MIN_INLIERS:
        return 0, 0.0
    knn = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(da, db, k=2)
    dys: list[float] = []
    for pair in knn:
        if len(pair) < 2:
            continue
        m, n = pair
        if m.distance < _RATIO * n.distance:          # Lowe ratio test
            xa, ya = ka[m.queryIdx].pt
            xb, yb = kb[m.trainIdx].pt
            if abs(xb - xa) < _DX_TOL:                # 近乎纯垂直
                dys.append(yb - ya)
    arr = np.array(dys)
    nz = arr[np.abs(arr) >= MIN_PROGRESS]             # 排除固定栏的 |dy|≈0 簇
    if len(nz) < _MIN_INLIERS:
        return 0, 0.0
    # 主导非零簇：4px 直方图找峰，峰 ± _CLUSTER_TOL 内取中位数
    lo, hi = int(nz.min()), int(nz.max())
    hist, edges = np.histogram(nz, bins=np.arange(lo - 1, hi + 5, 4))
    peak = edges[int(hist.argmax())] + 2
    inl = np.abs(nz - peak) < _CLUSTER_TOL
    if int(inl.sum()) < _MIN_INLIERS:
        return 0, 0.0
    shift = int(round(float(np.median(nz[inl]))))    # 带符号：<0 向下滚，>0 向上滚
    if abs(shift) < MIN_PROGRESS:
        return 0, 0.0
    return shift, round(float(inl.sum()) / len(nz), 2)


def _content_crop(
    png_bytes: bytes,
    *, content_top: float = CONTENT_TOP, content_bot: float = CONTENT_BOT,
) -> Image.Image:
    im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    w, h = im.size
    return im.crop((0, int(h * content_top), w, int(h * content_bot)))


def _vstack(top: Image.Image, bottom: Image.Image) -> Image.Image:
    out = Image.new("RGB", (top.width, top.height + bottom.height), "white")
    out.paste(top, (0, 0))
    out.paste(bottom, (0, top.height))
    return out


class StitchAccumulator:
    """逐帧喂入，几何拼接成长内容；累积到 chunk_px 就吐出一段供 reader 读（带 overlap）。

    用法：
        acc = StitchAccumulator(chunk_px=..., overlap_px=...)
        for frame in frames:
            for chunk_png, advanced in acc.feed(frame_png):  # 通常 0~1 段
                note = reader.read(chunk_png, ...)
        for chunk_png in [acc.flush()]:  # 收尾读剩余
            ...
    feed 返回值：(emitted_chunks, advanced)
      emitted_chunks: list[bytes] 本帧触发可读的 chunk PNG（一般 0 或 1 段）
      advanced: bool 本帧是否带来了新内容（= 拼接推进了），供 supervisor 边界判定用

    ``content_top``/``content_bot``/``frame_mask`` 由平台 adapter 注入（见模块头）。
    """

    def __init__(
        self, *, chunk_viewports: float = 1.0, overlap_px: int = 150,
        chunk_px: int | None = None,
        content_top: float = CONTENT_TOP, content_bot: float = CONTENT_BOT,
        frame_mask: np.ndarray | None = None,
    ) -> None:
        # chunk_px 可显式给定；否则首帧按「内容带高 × chunk_viewports」自动确定（≈一屏）。
        self._chunk_viewports = chunk_viewports
        self._chunk_px = chunk_px
        self._overlap_px = max(0, overlap_px)
        self._content_top = content_top
        self._content_bot = content_bot
        self._frame_mask = frame_mask
        self._canvas: Image.Image | None = None
        self._prev_gray: np.ndarray | None = None

    @property
    def pending_px(self) -> int:
        return 0 if self._canvas is None else self._canvas.height

    def _crop(self, png_bytes: bytes) -> Image.Image:
        return _content_crop(png_bytes, content_top=self._content_top, content_bot=self._content_bot)

    def feed(self, png_bytes: bytes) -> tuple[list[bytes], bool]:
        gray = _gray_u8(png_bytes)
        advanced = False
        chunks: list[bytes] = []
        if self._canvas is None:
            self._canvas = self._crop(png_bytes)
            self._prev_gray = gray
            advanced = True                       # 首帧整屏内容都是新的
            if self._chunk_px is None:            # 首帧按内容带高确定 chunk 尺寸（≈一屏）
                self._chunk_px = max(1, int(self._canvas.height * self._chunk_viewports))
        else:
            assert self._prev_gray is not None
            shift, _ = robust_shift(
                self._prev_gray, gray,
                content_top=self._content_top, content_bot=self._content_bot,
                frame_mask=self._frame_mask,
            )
            if shift < 0:                         # 有可信的向下滚 → 追加新条带
                content = self._crop(png_bytes)
                new_h = min(abs(shift), content.height)
                new_band = content.crop((0, content.height - new_h, content.width, content.height))
                self._canvas = _vstack(self._canvas, new_band)
                advanced = True
            elif _content_sim(
                self._prev_gray, gray,
                content_top=self._content_top, content_bot=self._content_bot,
            ) < REBASELINE_SIM:
                # shift==0 但两帧明显不同（换页 / 加载跳变 / 转场，无可追踪重叠）→ 重定基线：
                # 先把当前画布吐成 chunk（被读、不丢残留），再用这一新帧重开画布、整帧采下。
                if self._canvas.height > self._overlap_px:
                    chunks.append(_png(self._canvas))
                self._canvas = self._crop(png_bytes)
                advanced = True
            # else: shift==0 且两帧近乎相同 → 真没滚动/无效滚动，不追加（不重复采集同一屏）
            self._prev_gray = gray

        chunk_px = self._chunk_px or 1
        while self._canvas is not None and self._canvas.height >= chunk_px:
            chunk = self._canvas.crop((0, 0, self._canvas.width, chunk_px))
            chunks.append(_png(chunk))
            # 保留底部 overlap_px 作为下一段开头：chunk 间 overlap 像素完全相同，
            # 既不切断行，重复行也能被精确文本去重可靠剔除（同像素→同 OCR 文本）。
            keep_top = max(0, chunk_px - self._overlap_px)
            self._canvas = self._canvas.crop(
                (0, keep_top, self._canvas.width, self._canvas.height)
            )
        return chunks, advanced

    def flush(self) -> bytes | None:
        """收尾：吐出剩余未读内容（若多于保留的 overlap）。"""
        if self._canvas is None or self._canvas.height <= self._overlap_px:
            self._canvas = None
            return None
        chunk = _png(self._canvas)
        self._canvas = None
        return chunk


def _png(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()
