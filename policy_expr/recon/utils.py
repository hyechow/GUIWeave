"""Shared data classes, visualization, and output utilities for page recon."""

from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from policy_expr.recon.page_parser import ParsedPage


# ── Exceptions ────────────────────────────────────────────

class ProbeAbortedError(RuntimeError):
    """Raised when probe_elements cannot return to initial page after a tap."""
    def __init__(
        self,
        message: str,
        failed_tap: int,
        failed_element: str,
        back_attempts: list[dict],
    ):
        super().__init__(message)
        self.failed_tap = failed_tap
        self.failed_element = failed_element
        self.back_attempts = back_attempts  # list of {strategy, coords, score, success}


# ── Data classes ──────────────────────────────────────────

@dataclass
class TapResult:
    """Result of tapping one element on the page."""
    index: int
    element_type: str
    label: str
    x: float
    y: float
    tap_ok: bool
    screenshot_path: str
    navigated: bool = False
    back_attempts: list[dict] = field(default_factory=list)
    child_status: str = ""  # "new_explored" / "new_depth_limit" / "duplicate" / "error"
    identity: dict = field(default_factory=dict)


@dataclass
class ReconResult:
    """Full recon result for one page."""
    elements_count: int
    initial_screenshot_path: str = ""
    parent_page: str = ""
    taps: list[TapResult] = field(default_factory=list)

    def save(self, path: Path) -> None:
        data = {
            "parent_page": self.parent_page,
            "elements_count": self.elements_count,
            "initial_screenshot": self.initial_screenshot_path,
            "taps": [
                {
                    "index": t.index,
                    "element_type": t.element_type,
                    "label": t.label,
                    "x": t.x,
                    "y": t.y,
                    "tap_ok": t.tap_ok,
                    "navigated": t.navigated,
                    "screenshot": t.screenshot_path,
                    "back_attempts": t.back_attempts,
                    "child_status": t.child_status,
                    "identity": t.identity,
                }
                for t in self.taps
            ],
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2))


# ── Visualization ─────────────────────────────────────────

TYPE_COLOR: dict[str, tuple[int, int, int]] = {
    "back_button": (255, 80, 80),
    "tab": (80, 160, 255),
    "button": (80, 220, 80),
    "link": (255, 200, 0),
    "input": (200, 80, 255),
    "menu_item": (255, 140, 0),
    "icon": (0, 220, 220),
}
RADIUS = 12
SCREEN_MATCH_SIZE = 64
SCREEN_MATCH_THRESHOLD = 0.99
SCREEN_DIFFERENT_THRESHOLD = 0.97


@dataclass(frozen=True)
class ScreenMatchDecision:
    """Layered decision for whether a screen matches the initial page."""
    matched: bool | None
    similarity: float
    method: str
    reason: str


def png_similarity(png1: bytes, png2: bytes, size: int = SCREEN_MATCH_SIZE) -> float:
    """Return edge IoU between two PNG images (robust to dynamic content changes)."""
    from skimage.feature import canny

    img1 = np.array(Image.open(io.BytesIO(png1)).convert("L"), dtype=np.float64) / 255.0
    img2_raw = Image.open(io.BytesIO(png2)).convert("L")
    if img1.shape != img2_raw.size[::-1]:
        img2_raw = img2_raw.resize((img1.shape[1], img1.shape[0]))
    img2 = np.array(img2_raw, dtype=np.float64) / 255.0
    e1 = canny(img1).astype(np.float64)
    e2 = canny(img2).astype(np.float64)
    intersection = (e1 * e2).sum()
    union = (e1 + e2).clip(0, 1).sum()
    return float(intersection / union) if union > 0 else 0.0


def matches_initial(
    initial_png: bytes,
    current_png: bytes,
    threshold: float = SCREEN_MATCH_THRESHOLD,
) -> tuple[bool, float]:
    """Return whether current PNG is close enough to the initial screen."""
    similarity = png_similarity(initial_png, current_png)
    return similarity >= threshold, similarity


def decide_by_similarity(initial_png: bytes, current_png: bytes) -> ScreenMatchDecision:
    """Use only image similarity when the result is clear, otherwise defer."""
    similarity = png_similarity(initial_png, current_png)
    if similarity >= SCREEN_MATCH_THRESHOLD:
        return ScreenMatchDecision(True, similarity, "pixel", "similarity above match threshold")
    if similarity <= SCREEN_DIFFERENT_THRESHOLD:
        return ScreenMatchDecision(False, similarity, "pixel", "similarity below different threshold")
    return ScreenMatchDecision(None, similarity, "pixel", "similarity in uncertain band")


def same_page_by_structure(initial_page: ParsedPage, current_page: ParsedPage) -> tuple[bool, str]:
    """Compare parsed page structure via element overlap."""
    initial_labels = {
        (el.element_type, el.label.strip())
        for el in initial_page.interactive_elements
        if el.label.strip()
    }
    current_labels = {
        (el.element_type, el.label.strip())
        for el in current_page.interactive_elements
        if el.label.strip()
    }
    if initial_labels and current_labels:
        overlap = len(initial_labels & current_labels)
        union = len(initial_labels | current_labels)
        score = overlap / union
        if score >= 0.7:
            return True, f"element overlap {score:.2f}"

    return False, "different page structure"


def _font(size: int = 14) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype("/System/Library/Fonts/PingFang.ttc", size)
    except Exception:
        return ImageFont.load_default()


GROUP_PALETTE = [
    (231, 76, 60),    # red
    (46, 204, 113),   # green
    (52, 152, 219),   # blue
    (241, 196, 15),   # yellow
    (155, 89, 182),   # purple
    (230, 126, 34),   # orange
    (26, 188, 156),   # teal
    (236, 100, 165),  # pink
    (52, 73, 94),     # dark blue
    (127, 140, 141),  # gray
    (192, 57, 43),    # dark red
    (39, 174, 96),    # dark green
    (41, 128, 185),   # dark blue
    (243, 156, 18),   # dark yellow
    (142, 68, 173),   # dark purple
]


def visualize(page: ParsedPage, png_bytes: bytes) -> bytes:
    """Draw element markers on screenshot, return annotated PNG bytes."""
    img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    w, h = img.size
    draw = ImageDraw.Draw(img, "RGBA")

    for i, el in enumerate(page.interactive_elements, 1):
        cx = int(el.x / 1000 * w)
        cy = int(el.y / 1000 * h)
        is_yolo_extra = (
            el.label == "" and el.element_type == "icon" and el.leads_to == ""
        )
        if is_yolo_extra:
            draw.polygon(
                [(cx, cy - RADIUS), (cx + RADIUS, cy), (cx, cy + RADIUS), (cx - RADIUS, cy)],
                fill=(180, 180, 180, 160),
                outline=(255, 255, 255, 255),
            )
        else:
            color = TYPE_COLOR.get(el.element_type, (200, 200, 200))
            draw.ellipse(
                [cx - RADIUS, cy - RADIUS, cx + RADIUS, cy + RADIUS],
                fill=(*color, 200),
                outline=(255, 255, 255, 255),
                width=2,
            )

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def visualize_yolo(
    png_bytes: bytes,
    boxes: list,
    img_w: int,
    img_h: int,
) -> bytes:
    """Draw YOLO detected icon bboxes on screenshot."""
    img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    draw = ImageDraw.Draw(img, "RGBA")

    for b in boxes:
        draw.rectangle(
            [int(b.x1), int(b.y1), int(b.x2), int(b.y2)],
            outline=(0, 220, 220, 255),
            width=2,
        )

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def visualize_areas(
    png_bytes: bytes,
    areas: list,
) -> bytes:
    """Draw area markers on screenshot."""
    img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    w, h = img.size
    draw = ImageDraw.Draw(img, "RGBA")

    for ai, area in enumerate(areas):
        color = GROUP_PALETTE[ai % len(GROUP_PALETTE)]
        cx = int(area.center_xy[0] / 1000 * w)
        cy = int(area.center_xy[1] / 1000 * h)
        draw.ellipse(
            [cx - RADIUS, cy - RADIUS, cx + RADIUS, cy + RADIUS],
            fill=(*color, 200),
            outline=(255, 255, 255, 255),
            width=2,
        )

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def save_llm_prompt_debug(
    debug_path: Path,
    system_prompt: str,
    context_text: str,
    before_b64: str,
    after_b64: str,
    response: dict | None,
) -> None:
    """Save an HTML visualization of an LLM prompt + response for debugging.

    response keys (all optional): page_type, can_go_back, method, back_x, back_y.
    """
    import html as _html

    def _esc(s: str) -> str:
        return _html.escape(str(s))

    if response is not None:
        tap_x = round(response.get("back_x", -1))
        tap_y = round(response.get("back_y", -1))
        left, top = tap_x / 10, tap_y / 10
        crosshair = (
            f'<div class="crosshair" style="left:{left:.1f}%;top:{top:.1f}%">'
            '<div class="ch-h"></div><div class="ch-v"></div>'
            '<div class="ch-ring"></div></div>'
        )
        response_html = f"""
        <div class="section response-section">
          <div class="section-title">LLM 输出</div>
          <div class="response-grid">
            <div class="resp-item"><span class="resp-key">page_type</span><span class="resp-val type-badge">{_esc(response.get("page_type", ""))}</span></div>
            <div class="resp-item"><span class="resp-key">can_go_back</span><span class="resp-val">{_esc(response.get("can_go_back", ""))}</span></div>
            <div class="resp-item"><span class="resp-key">method</span><span class="resp-val">{_esc(response.get("method", ""))}</span></div>
            <div class="resp-item"><span class="resp-key">坐标</span><span class="resp-val">({tap_x}, {tap_y})</span></div>
          </div>
          <div class="tap-preview">
            <div class="ss-wrap">
              <div class="ss-label after-label">AFTER + tap point</div>
              <img src="data:image/png;base64,{after_b64}">
              {crosshair}
            </div>
          </div>
        </div>"""
    else:
        response_html = (
            '<div class="section response-section">'
            '<div class="section-title">LLM 输出</div>'
            '<span class="resp-val" style="color:#ff5555">can_go_back=False 或坐标越界</span>'
            '</div>'
        )

    page = f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<title>LLM prompt debug</title>
<style>
  body {{ font-family: -apple-system, sans-serif; background: #1a1a2e; color: #eee; margin: 0; padding: 20px; font-size: 13px; }}
  h1 {{ font-size: 15px; color: #888; margin: 0 0 16px; }}
  .section {{ background: #252540; border-radius: 8px; padding: 14px 16px; margin-bottom: 14px; }}
  .section-title {{ font-size: 11px; font-weight: 700; color: #888; text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 10px; }}
  pre {{ background: #1a1a2e; padding: 12px; border-radius: 6px; overflow-x: auto; font-size: 12px; line-height: 1.5; color: #ccc; white-space: pre-wrap; word-break: break-word; margin: 0; }}
  .images {{ display: flex; gap: 16px; align-items: flex-start; }}
  .ss-wrap {{ position: relative; flex-shrink: 0; }}
  .ss-wrap img {{ height: 300px; border-radius: 6px; display: block; }}
  .ss-label {{ font-size: 10px; font-weight: 700; padding: 2px 8px; border-radius: 4px; margin-bottom: 4px; display: inline-block; }}
  .before-label {{ background: #0a84ff; color: #fff; }}
  .after-label {{ background: #ff9500; color: #fff; }}
  .response-section {{ border-left: 3px solid #34C759; }}
  .response-grid {{ display: flex; flex-wrap: wrap; gap: 10px 24px; margin-bottom: 12px; }}
  .resp-item {{ display: flex; align-items: center; gap: 8px; }}
  .resp-key {{ color: #888; font-size: 12px; }}
  .resp-val {{ font-family: monospace; font-weight: 600; color: #eee; }}
  .type-badge {{ background: #ff9500; color: #000; padding: 1px 10px; border-radius: 10px; }}
  .tap-preview {{ display: flex; gap: 16px; }}
  .crosshair {{ position: absolute; pointer-events: none; z-index: 1; }}
  .ch-h, .ch-v {{ position: absolute; background: rgba(255,255,0,0.85); }}
  .ch-h {{ width: 40px; height: 2px; top: -1px; left: -20px; }}
  .ch-v {{ width: 2px; height: 40px; left: -1px; top: -20px; }}
  .ch-ring {{ position: absolute; width: 14px; height: 14px; border: 2px solid rgba(255,255,0,0.9); border-radius: 50%; top: -7px; left: -7px; }}
</style>
</head>
<body>
  <h1>LLM prompt · {_esc(debug_path.stem)}</h1>

  <div class="section">
    <div class="section-title">System Prompt</div>
    <pre>{_esc(system_prompt)}</pre>
  </div>

  <div class="section">
    <div class="section-title">Human Message · 上下文</div>
    <pre>{_esc(context_text)}</pre>
  </div>

  <div class="section">
    <div class="section-title">Human Message · 截图</div>
    <div class="images">
      <div>
        <div class="ss-label before-label">BEFORE</div>
        <div class="ss-wrap"><img src="data:image/png;base64,{before_b64}"></div>
      </div>
      <div>
        <div class="ss-label after-label">AFTER</div>
        <div class="ss-wrap"><img src="data:image/png;base64,{after_b64}"></div>
      </div>
    </div>
  </div>

  {response_html}
</body>
</html>"""
    debug_path.write_text(page, encoding="utf-8")


def make_nav_context(label: str, element_type: str) -> str:
    """Build a nav_context string describing what triggered navigation."""
    if element_type == "tab":
        return f"点击了底部tab「{label}」"
    return f"点击了{element_type}「{label}」"


def print_areas(knowledge: "PageKnowledge") -> None:  # noqa: F821
    """Print areas to stdout."""
    # print(f"  应用 : {knowledge.page.app_name}")
    # print(f"  页面 : {knowledge.page.page_title}")
    # print(f"  指纹 : {knowledge.page.signature}")
    # print(f"  区域数 : {len(knowledge.areas)}")
    # for i, a in enumerate(knowledge.areas, 1):
    #     print(f"    [{i:2d}] ({a.center_xy[0]:5.0f},{a.center_xy[1]:5.0f})  "
    #           f"「{a.label}」→ {a.target_page}")


def viz_result(
    knowledge: "PageKnowledge",  # noqa: F821
    png_bytes: bytes,
    stem: str,
    out_dir: Path,
) -> None:
    """Save area JSON + LLM/YOLO/area visualizations."""
    out_dir.mkdir(parents=True, exist_ok=True)
    print_areas(knowledge)

    output = knowledge.model_dump(mode="json")
    output["page"].pop("interactive_elements", None)

    json_path = out_dir / f"{stem}_result.json"
    json_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")

    # LLM-only visualization
    if knowledge.llm_page:
        llm_viz_path = out_dir / f"{stem}_llm_viz.png"
        llm_viz_path.write_bytes(visualize(knowledge.llm_page, png_bytes))
        # print(f"  LLM 可视化 : {llm_viz_path}")

    # YOLO-only visualization
    if knowledge.yolo_boxes and knowledge.img_size:
        yolo_viz_path = out_dir / f"{stem}_yolo_viz.png"
        yolo_viz_path.write_bytes(visualize_yolo(
            png_bytes, knowledge.yolo_boxes,
            knowledge.img_size[0], knowledge.img_size[1],
        ))
        # print(f"  YOLO 可视化 : {yolo_viz_path}")

    # Area visualization
    areas_viz_path = out_dir / f"{stem}_areas_viz.png"
    areas_viz_path.write_bytes(visualize_areas(png_bytes, knowledge.areas))

    # print(f"  JSON : {json_path}")
    # print(f"  区域可视化 : {areas_viz_path}")
