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
from policy_expr.config import resolve_llm_config
from policy_expr.schemas import TargetVerify

_SYSTEM = """你是一个 GUI 操作的「落点校验器」。截图上有一个红色圆环+十字标记，表示即将点击的位置。
给你一条操作指令，判断这个标记是否**正好落在指令想点的目标元素上**。
- on_target=true 仅当标记中心压在目标元素本体上。
- 若标记落在相邻/其它元素上（哪怕很近），on_target=false，并在 actual_element 写出标记实际压住的是什么。
只看标记的实际位置，不要脑补指令“应该”点哪。reason 一句话。"""


def render_marker(png: bytes, nx: float, ny: float) -> bytes:
    """Draw a hollow ring + crosshair at normalized (0-1000) point on the frame.

    Hollow so the element under the marker stays visible to the verifier.
    """
    img = Image.open(io.BytesIO(png)).convert("RGB")
    w, h = img.size
    px, py = nx / 1000 * w, ny / 1000 * h
    d = ImageDraw.Draw(img)
    r = max(18, int(w * 0.035))
    for off in (0, 1, 2):  # thicken ring
        d.ellipse([px - r + off, py - r + off, px + r - off, py + r - off], outline=(255, 0, 0))
    d.line([px - r - 8, py, px + r + 8, py], fill=(255, 0, 0), width=2)
    d.line([px, py - r - 8, px, py + r + 8], fill=(255, 0, 0), width=2)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _verify_llm() -> ChatOpenAI:
    # Reuse the action_policy vision model (proven for this task). A dedicated
    # cheaper "target_verify" model can be wired later via its own config key.
    cfg = resolve_llm_config("action_policy")
    return ChatOpenAI(model=cfg.model, api_key=cfg.api_key, base_url=cfg.base_url, temperature=0)


def verify_target(png: bytes, snapped_x: float, snapped_y: float, instruction: str) -> TargetVerify:
    """Render the snapped point on the frame and judge whether it is on target."""
    marked = render_marker(png, snapped_x, snapped_y)
    b64 = base64.b64encode(marked).decode()
    msgs = [
        SystemMessage(content=_SYSTEM),
        HumanMessage(content=[
            {"type": "text", "text": f"操作指令：「{instruction}」。红色标记落在目标元素上吗？"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]),
    ]
    return invoke_structured(_verify_llm(), msgs, TargetVerify)
