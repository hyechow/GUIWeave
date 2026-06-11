"""Post-action targeting verify.

After a tap is dispatched, render the snapped tap point as a marker on the
pre-action frame and ask a light vision LLM whether it landed on the element
the instruction intended. Runs concurrently with the post-action settle, so it
adds ~no silent latency; the result is carried to the next turn where off_target
routes straight into replan (see runner + MilestoneSupervisorPolicy).

It catches the "screen changed but to the wrong element" failure that SimStuck
(screen-frozen) cannot — e.g. a 搜索框 tap that mis-hit the 转账 tab.
"""
from __future__ import annotations

import base64
import io

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from PIL import Image, ImageDraw

from llm.structured import invoke_structured
from gui_agent.core.config import resolve_llm_config
from gui_agent.core.schemas import TargetVerify

_SYSTEM = """你是一个 GUI 操作的「落点校验器」。截图上有一个红色圆环+十字标记，表示刚刚点击的位置。
给你一条操作指令，判断标记**十字中心**是否正好压在指令想点的目标元素本体上。

务必按此顺序，先描述、后判断，不要先看指令下结论：
1. 只盯着红色十字的正中心，说出它正下方紧贴压住的是哪一个具体 UI 元素——读出该元素上的文字/图标，
   写进 actual_element。注意它到底属于哪一行/哪一层：是内容区里的按钮、chip、列表条目，还是屏幕最底缘那一排 Tab？
2. 再判断 actual_element 是不是指令要点的目标。完全是同一个元素 → on_target=true；否则 on_target=false。

硬性规则：
- 只认十字中心的真实位置，绝不能因为「指令说要点 X」就把标记脑补成落在 X 上。
- 底部 Tab 栏紧贴屏幕最底缘。判断垂直高度：只要十字明显在最底那排 Tab 的**上方**（哪怕只高出一点点、
  落在内容区的按钮/chip/列表项上），就不是 Tab，必须 off_target——即使水平方向与某个 Tab 同列。
- ⚠️ 底部 Tab 栏有多个并排 Tab，每个 Tab 在图标下方有自己的文字标签。**当前高亮的那个 Tab（通常变蓝/
  加深，是当前所在页）和顶部页面标题，都是干扰项**——十字压住的 Tab 往往不是高亮那个。必须**按十字的水平
  位置，逐字读出它正下方那一个 Tab 自己的文字标签**写进 actual_element，绝不能拿高亮 Tab 或页面标题的名字
  顶替它（例：当前在「世界时钟」页，不代表十字下方最左那个 Tab 就叫「世界时钟」——它可能是「闹钟」）。
  读不清时用它在 Tab 栏里**从左数第几个**+图标形状判断，不要用页面语境猜。
- 标记落在目标相邻的元素上（上下相邻行、左右相邻列），哪怕很近，一律 off_target。
reason 一句话，说明十字中心实际压住了什么。"""


def render_marker(png: bytes, nx: float, ny: float) -> bytes:
    """Draw a hollow ring + center-gapped crosshair at normalized (0-1000) point.

    The crosshair has a GAP at the center so a small, low-contrast element under
    the exact point stays visible to the verifier. A solid crosshair through the
    center occludes small targets (e.g. the home-screen search capsule), which
    forced the judge to fall back on the bottom-Tab rule and reject correct hits
    (false off_target). The four inward stubs + ring still pinpoint the center.
    """
    img = Image.open(io.BytesIO(png)).convert("RGB")
    w, h = img.size
    px, py = nx / 1000 * w, ny / 1000 * h
    d = ImageDraw.Draw(img)
    r = max(14, int(w * 0.030))
    gap = max(12, int(w * 0.022))   # clear center window so small targets show through
    for off in (0, 1, 2):  # thicken ring
        d.ellipse([px - r + off, py - r + off, px + r - off, py + r - off], outline=(255, 0, 0))
    # crosshair as four inward stubs, leaving the central [-gap, gap] window clear
    d.line([px - r - 8, py, px - gap, py], fill=(255, 0, 0), width=2)
    d.line([px + gap, py, px + r + 8, py], fill=(255, 0, 0), width=2)
    d.line([px, py - r - 8, px, py - gap], fill=(255, 0, 0), width=2)
    d.line([px, py + gap, px, py + r + 8], fill=(255, 0, 0), width=2)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _verify_llm() -> ChatOpenAI:
    # Reuse the action_policy vision model (proven for this task). A dedicated
    # cheaper "target_verify" model can be wired later via its own config key.
    cfg = resolve_llm_config("action_policy")
    return ChatOpenAI(model=cfg.model, api_key=cfg.api_key, base_url=cfg.base_url, temperature=0)


def _upscale(png: bytes, min_w: int = 900) -> bytes:
    """Upscale a small frame so tiny labels (bottom-Tab text) are legible to the verifier.

    The agent's observation is downscaled (~320px wide) to save tokens; at that size the
    bottom-Tab labels are barely legible, and the marker ring can cover the target's own
    label — so the judge falls back on page context and misreads the Tab. Upscaling with a
    smooth filter gives the model bigger glyphs to OCR. No-op when already large enough."""
    img = Image.open(io.BytesIO(png)).convert("RGB")
    if img.width >= min_w:
        return png
    scale = min_w / img.width
    img = img.resize((min_w, int(img.height * scale)), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def verify_target(png: bytes, snapped_x: float, snapped_y: float, instruction: str) -> TargetVerify:
    """Render the snapped point on the frame and judge whether it is on target."""
    marked = render_marker(_upscale(png), snapped_x, snapped_y)
    b64 = base64.b64encode(marked).decode()
    msgs = [
        SystemMessage(content=_SYSTEM),
        HumanMessage(content=[
            {"type": "text", "text": f"操作指令：「{instruction}」。红色标记落在目标元素上吗？"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]),
    ]
    return invoke_structured(_verify_llm(), msgs, TargetVerify)
