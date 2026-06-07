"""Test: full-screen popup detection pipeline (pixel → LLM → YOLO)."""

import io
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from gui_agent.adapters.iphone.overlay_detect import detect_fullscreen_popup
from gui_agent.adapters.iphone.recon.popup_nav import llm_locate_close, yolo_snap


def _draw_cross(draw: ImageDraw.ImageDraw, x: float, y: float,
                w: int, h: int, color: tuple, R: int = 18) -> None:
    cx, cy = int(x / 1000 * w), int(y / 1000 * h)
    draw.ellipse([cx - R, cy - R, cx + R, cy + R], outline=color, width=3)
    draw.line([cx - R, cy, cx + R, cy], fill=color, width=2)
    draw.line([cx, cy - R, cx, cy + R], fill=color, width=2)


def test(img_path: str) -> None:
    png = Path(img_path).read_bytes()

    # Pixel pre-check
    img_arr = np.array(Image.open(io.BytesIO(png)).convert("RGB"), dtype=np.float32)
    h_px, w_px = img_arr.shape[:2]
    W = 40
    edge_mean = float(np.mean([img_arr[:W].mean(), img_arr[-W:].mean(),
                               img_arr[:, :W].mean(), img_arr[:, -W:].mean()]))
    center_mean = float(img_arr[h_px // 4:3 * h_px // 4, w_px // 4:3 * w_px // 4].mean())
    ratio = edge_mean / max(center_mean, 1.0)
    is_popup = detect_fullscreen_popup(img_arr)
    print(f"dimming    : edge={edge_mean:.1f} center={center_mean:.1f} "
          f"ratio={ratio:.3f} → {'弹窗' if is_popup else '非弹窗'}")

    if not is_popup:
        return

    # LLM locate
    llm_xy = llm_locate_close(png)
    if llm_xy is None:
        print("LLM        : 未能定位关闭按钮")
        return
    print(f"LLM close  : ({llm_xy[0]:.0f}, {llm_xy[1]:.0f})")

    # YOLO snap
    final_xy = yolo_snap(png, *llm_xy)
    snapped = final_xy != llm_xy
    print(f"YOLO snap  : ({final_xy[0]:.0f}, {final_xy[1]:.0f})"
          f"{'  ← 校正' if snapped else '  (未命中，使用 LLM 坐标)'}")

    # Visualize
    img = Image.open(io.BytesIO(png)).convert("RGB")
    w, h = img.size
    draw = ImageDraw.Draw(img)
    _draw_cross(draw, llm_xy[0], llm_xy[1], w, h, (255, 165, 0))   # 橙 = LLM
    if snapped:
        _draw_cross(draw, final_xy[0], final_xy[1], w, h, (0, 200, 0))  # 绿 = YOLO

    out = Path(tempfile.gettempdir()) / "popup_close_viz.png"
    img.save(out)
    subprocess.Popen(["open", str(out)])
    print(f"标注图     : {out}  (橙=LLM, 绿=YOLO校正)")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else \
        "logs/recon/拼多多/浏览电商商品与促销信息/tap/tap_02_充值中心.png"
    test(path)
