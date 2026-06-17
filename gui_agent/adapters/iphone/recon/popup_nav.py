"""Full-screen popup detection and close: pixel pre-check → LLM locate → YOLO snap → tap."""

from __future__ import annotations

import base64
import io
import time
from collections.abc import Callable

import numpy as np
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from PIL import Image
from pydantic import BaseModel, Field

from llm.structured import invoke_structured
from gui_agent.core.config import resolve_llm_config
from gui_agent.adapters.iphone.executor import logical_xy
from gui_agent.adapters.iphone.overlay_detect import detect_fullscreen_popup
from gui_agent.core.policies.base import resize_to_logical_png
_POPUP_PROMPT = """\
你是一个 iPhone 页面分析专家。
分析截图，找到全屏弹窗（广告弹窗、全屏促销弹窗）的关闭按钮（× 或「关闭」或「跳过」）的位置。
坐标系：左上角 (0,0)，右下角 (1000,1000)。
"""


class _PopupClose(BaseModel):
    close_x: float = Field(description="关闭按钮中心的归一化 x 坐标（0-1000）")
    close_y: float = Field(description="关闭按钮中心的归一化 y 坐标（0-1000）")
    description: str = Field(description="简要描述弹窗内容和关闭按钮位置")


def llm_locate_close(png: bytes) -> tuple[float, float] | None:
    """Call LLM to locate the close button. Returns (x, y) in 0-1000 coords or None."""
    cfg = resolve_llm_config("recon.navigator")
    llm = ChatOpenAI(model=cfg.model, api_key=cfg.api_key,
                     base_url=cfg.base_url, temperature=0)
    b64 = base64.b64encode(resize_to_logical_png(png)).decode()
    messages = [
        SystemMessage(content=_POPUP_PROMPT),
        HumanMessage(content=[
            {"type": "text", "text": "请找到关闭按钮的位置："},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]),
    ]
    try:
        result = invoke_structured(llm, messages, _PopupClose)
        print(f"  [popup] LLM: ({result.close_x:.0f}, {result.close_y:.0f}) — {result.description}")
        return result.close_x, result.close_y
    except Exception as e:
        print(f"  [popup] LLM 调用失败: {e}")
        return None


def yolo_snap(png: bytes, x: float, y: float) -> tuple[float, float]:
    """Snap (x, y) to nearest YOLO icon center if within 100px, else return as-is."""
    try:
        from gui_agent.adapters.iphone.recon.yolo_calibrator import YoloCalibrator
        cal = YoloCalibrator.from_png(png)
        if cal is not None:
            snapped = cal.nearest(x, y, max_dist=100.0)
            if snapped:
                print(f"  [popup] YOLO snap: ({snapped[0]:.0f}, {snapped[1]:.0f})")
                return snapped
    except Exception:
        pass
    return x, y


def close_popup(
    client,
    screenshot_fn: Callable[[], bytes],
    current_png: bytes | None = None,
    threshold: float = 0.25,
) -> bool:
    """Detect and close a full-screen popup.

    Runs a cheap pixel check first; calls LLM + YOLO only when a popup is detected.
    Returns True if a popup was detected and a close tap was attempted.
    """
    png = current_png if current_png is not None else screenshot_fn()

    img = np.array(Image.open(io.BytesIO(png)).convert("RGB"), dtype=np.float32)
    if not detect_fullscreen_popup(img, threshold):
        return False

    h, w = img.shape[:2]
    W = 40
    edge_mean = float(np.mean([img[:W].mean(), img[-W:].mean(),
                               img[:, :W].mean(), img[:, -W:].mean()]))
    center_mean = float(img[h // 4:3 * h // 4, w // 4:3 * w // 4].mean())
    ratio = edge_mean / max(center_mean, 1.0)
    print(f"  [popup] 全屏弹窗 (ratio={ratio:.3f})，定位关闭按钮...")

    xy = llm_locate_close(png)
    if xy is None:
        return True  # Detected but couldn't locate close; caller decides how to handle

    x, y = yolo_snap(png, *xy)
    lx, ly = logical_xy(x, y)
    print(f"  [popup] 点击关闭 ({x:.0f}, {y:.0f}) → 逻辑({lx:.0f}, {ly:.0f})")
    client.tap(lx, ly)
    time.sleep(1.5)
    return True
