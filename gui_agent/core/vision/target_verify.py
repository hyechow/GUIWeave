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

from gui_agent.core.config import resolve_llm_config
from gui_agent.core.schemas import TargetVerify
from gui_agent.prompts import load_prompt_text
from llm.structured import invoke_structured

_SYSTEM = load_prompt_text("task.vision.target_verify")


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
