"""Post-action targeting verify.

After a spatial action is dispatched, render its final point as a marker on the
pre-action frame and ask a light vision LLM whether it landed on the element
the instruction intended. Runs concurrently with the post-action settle, so it
adds ~no silent latency; the result reaches the next turn so the Worker can
avoid repeating a visibly off-target action.

It catches the "screen changed but to the wrong element" failure that SimStuck
(screen-frozen) cannot detect.
"""

from __future__ import annotations

import base64
import io

from langchain_core.messages import HumanMessage, SystemMessage
from PIL import Image, ImageDraw

from gui_agent.core.config import resolve_llm_config
from gui_agent.core.runtime.action_settle import VERIFY_TIMEOUT_S
from gui_agent.core.schemas import (
    BaseActionDecision,
    TargetGrounding,
    TargetVerify,
)
from gui_agent.prompts import load_prompt_text
from llm.provider_config import build_chat_model
from llm.structured import invoke_structured

_SYSTEM = load_prompt_text("task.vision.target_verify")
_GROUND_SYSTEM = load_prompt_text("task.vision.target_grounding")


def render_marker(png: bytes, nx: float, ny: float) -> bytes:
    """Draw a hollow ring + center-gapped crosshair at normalized (0-1000) point.

    Leave the center clear so a small, low-contrast target remains visible.
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


def _vision_llm():
    cfg = resolve_llm_config("target_verify")
    return build_chat_model(
        cfg,
        timeout=min(cfg.timeout_s, max(1, VERIFY_TIMEOUT_S - 1)),
        max_retries=0,
    )


def _upscale(png: bytes, min_w: int = 900) -> bytes:
    """Upscale a small frame so tiny target labels remain legible."""
    img = Image.open(io.BytesIO(png)).convert("RGB")
    if img.width >= min_w:
        return png
    scale = min_w / img.width
    img = img.resize((min_w, int(img.height * scale)), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def crop_target_region(
    png: bytes,
    proposed_x: float,
    proposed_y: float,
    *,
    half_width: float = 400.0,
    half_height: float = 180.0,
) -> tuple[bytes, tuple[float, float, float, float]]:
    """Crop a candidate-local region and return its full-frame normalized bounds."""

    img = Image.open(io.BytesIO(png)).convert("RGB")
    left = max(0.0, proposed_x - half_width)
    top = max(0.0, proposed_y - half_height)
    right = min(1000.0, proposed_x + half_width)
    bottom = min(1000.0, proposed_y + half_height)
    pixel_box = (
        round(left / 1000 * img.width),
        round(top / 1000 * img.height),
        round(right / 1000 * img.width),
        round(bottom / 1000 * img.height),
    )
    cropped = img.crop(pixel_box)
    output = io.BytesIO()
    cropped.save(output, format="PNG")
    return output.getvalue(), (left, top, right, bottom)


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
    return invoke_structured(
        _vision_llm(), msgs, TargetVerify, fallback_on_invalid=False,
    )


def ground_target(
    png: bytes,
    proposed_x: float,
    proposed_y: float,
    instruction: str,
    action_type: str,
) -> TargetGrounding:
    """Locate one intended control and its interactive bounds on the current frame."""

    local_png, crop_bounds = crop_target_region(png, proposed_x, proposed_y)
    crop_left, crop_top, crop_right, crop_bottom = crop_bounds
    local_x = (proposed_x - crop_left) / (crop_right - crop_left) * 1000
    local_y = (proposed_y - crop_top) / (crop_bottom - crop_top) * 1000
    marked = render_marker(_upscale(local_png, min_w=540), local_x, local_y)
    b64 = base64.b64encode(marked).decode()
    msgs = [
        SystemMessage(content=_GROUND_SYSTEM),
        HumanMessage(content=[
            {
                "type": "text",
                "text": (
                    f"操作类型：{action_type}。操作指令：「{instruction}」。"
                    "当前图片是候选点附近的局部裁剪；红色标记仅用于显示候选点。"
                    "请在这张局部图中定位指令指定控件本体的可交互边界。"
                ),
            },
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]),
    ]
    grounding = invoke_structured(
        _vision_llm(), msgs, TargetGrounding, fallback_on_invalid=False,
    )
    if grounding.target_box is None:
        return grounding
    left, top, right, bottom = grounding.target_box
    width, height = crop_right - crop_left, crop_bottom - crop_top
    return grounding.model_copy(update={"target_box": (
        crop_left + left / 1000 * width,
        crop_top + top / 1000 * height,
        crop_left + right / 1000 * width,
        crop_top + bottom / 1000 * height,
    )})


def resolve_target_grounding(
    decision: BaseActionDecision,
    grounding: TargetGrounding,
) -> tuple[BaseActionDecision, dict[str, str] | None, bool, str]:
    """Resolve one grounding result into correction, signal, or rejection."""

    action = decision.action
    box = grounding.target_box
    inside = bool(box and (
        box[0] - 6 <= float(action.x or 0) <= box[2] + 6
        and box[1] - 6 <= float(action.y or 0) <= box[3] + 6
    ))
    kind = grounding.control_type.casefold().replace("-", "_")
    accepted_kinds = {
        "type": ("input", "textbox", "editor", "textarea"),
        "select_option": ("select", "combobox", "dropdown", "option"),
    }.get(action.action_type)
    compatible = not accepted_kinds or any(token in kind for token in accepted_kinds)
    identity_supported = bool(
        grounding.label.strip() or grounding.container_context.strip()
    )
    correctable = bool(
        box and grounding.confidence == "high" and compatible
        and (inside or identity_supported)
    )
    actual = grounding.label or grounding.control_type
    signal = None
    if correctable:
        assert box is not None
        center = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
        if action.action_type == "type" or not inside:
            grounded = action.model_copy(update={
                "x": center[0],
                "y": center[1],
                "snap": {
                    "method": "visual_target_grounding",
                    "original": [action.x, action.y],
                    "snapped": list(center),
                    "target_box": list(box),
                    "confidence": grounding.confidence,
                    "info": actual or "visual target",
                },
            })
            decision = decision.model_copy(update={"action": grounded})
        signal = {
            "status": "on_target",
            "actual_element": actual,
            "reason": "high-confidence visual grounding located the intended control",
        }
    elif grounding.confidence == "medium" and inside and compatible:
        signal = {
            "status": "on_target",
            "actual_element": actual,
            "reason": "the proposed point lies inside the visually grounded target",
        }
    if signal is not None and grounding.container_context.strip():
        signal["container_context"] = grounding.container_context.strip()
    # Reject only when a control is actually detected at the point and it is not a
    # compatible text target (so a `type` aimed at a detected button/icon is blocked).
    # A point over an undetected region (`target_found=False`, e.g. a blank composer
    # body) carries no detected control to conflict with, so it is admitted and left to
    # the downstream red-marker verifier to confirm rather than being hard-rejected.
    reject = signal is None and grounding.target_found and (
        action.action_type == "type"
        or (
            identity_supported
            and grounding.confidence in {"high", "medium"}
        )
    )
    detail = f" for {actual!r}" if actual else ""
    reason = f": {grounding.reason}" if grounding.reason else ""
    error = (
        f"predispatch visual grounding could not confirm the proposed target{detail}{reason}"
        if reject else ""
    )
    return decision, signal, inside, error
