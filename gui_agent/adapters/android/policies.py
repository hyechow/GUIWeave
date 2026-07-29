"""Vision-only Android action policy: screenshot + instruction -> one Action.

Mirrors the iphone ``StructuredOutputPolicy`` LLM machinery — same config via
``resolve_llm_config('action_policy')``, same structured-output call via
``invoke_structured`` into an ``ActionDecision``, same ``BaseActionPolicy`` base —
but with an ANDROID system prompt: operate a phone touchscreen, output ONE action
within the android action vocabulary (tap / type / clear_text / press_enter /
scroll / drag / home / back / app_switch).

VISION-ONLY: the screenshot is sent as-is (downscaled only if very large) — it is
NOT the iphone 2x retina image, so ``resize_to_logical_png`` is deliberately NOT
used. Picker wheels are still vision-only, but planner hints are consumed here and
post-processed into deterministic Android ``scroll`` actions.

PROMPT HYGIENE: the system prompt lists ONLY what Android has (positive vocabulary
+ Android idioms — 三大金刚键 / 通知栏 / 底部导航 / 软键盘). It does NOT contain
cross-platform negations ("这不是 iPhone / 没有 picker / 不要输出 value_direction"):
those leak iphone field names into the prompt, raise their salience, and pollute it.
The shared schema's iphone-only fields are simply never described here, so the model
does not fill them; the executor ignores them anyway.
"""

from __future__ import annotations

import io
import re
from typing import Optional

from dotenv import load_dotenv

from gui_agent.adapters.android.actions import AndroidActionDecision
from gui_agent.core.policies.base import BaseActionPolicy
from gui_agent.prompts import load_prompt_text

load_dotenv()


SYSTEM_PROMPT = load_prompt_text("task.action_policy.android")

# Above this longest-edge size, downscale the screenshot before sending to the
# vision model (cost / latency). Android screencap is the device's own pixels, not
# a fixed 2x retina, so we do NOT halve unconditionally — only cap huge captures.
_MAX_EDGE = 1600

# Normalized anchors for the common Android time/date wheel layout. The y anchor
# sits inside the selected wheel band; it must not drift down into the settings
# list below the picker.
_PICKER_COLUMN_ANCHORS: dict[str, tuple[float, float]] = {
    "period": (230.0, 240.0),
    "ampm": (230.0, 240.0),
    "hour": (500.0, 240.0),
    "minute": (765.0, 240.0),
    "year": (250.0, 240.0),
    "month": (500.0, 240.0),
    "day": (750.0, 240.0),
}

_PICKER_VALUE_TO_SCROLL_DIRECTION = {
    # Observed on Android wheel pickers: scroll down (finger swipes up) increases
    # the selected value; scroll up (finger swipes down) decreases it.
    "increase": "down",
    "decrease": "up",
}

_PICKER_WORDS = (
    "picker",
    "滚轮",
    "选择器",
    "小时列",
    "分钟列",
    "时段列",
    "上午/下午",
)
_PICKER_ADJUST_WORDS = ("调整", "调到", "调至", "设置为", "设为", "改为")
_CLOCK_MINUTE_RE = re.compile(r"(?:\bminute\b|分钟|\d{1,2}\s*分(?:钟)?\b)", re.IGNORECASE)
_CLOCK_HOUR_RE = re.compile(r"(?:\bhour\b|小时|\d{1,2}\s*点(?:钟)?\b)", re.IGNORECASE)


def _is_tap_only_instruction(instruction: str) -> bool:
    return (
        any(w in instruction for w in ("点击", "tap", "轻触"))
        and not any(w in instruction for w in ("拖动", "拖拽", "滑动", "滚动", "调整", "调到", "调至"))
    )


def _looks_like_picker_adjust(instruction: str) -> bool:
    if any(w in instruction for w in _PICKER_WORDS):
        return True
    has_time_column = (
        any(w in instruction for w in ("上午", "下午"))
        or _CLOCK_MINUTE_RE.search(instruction) is not None
        or _CLOCK_HOUR_RE.search(instruction) is not None
    )
    return has_time_column and any(w in instruction for w in _PICKER_ADJUST_WORDS)


def _infer_picker_column(instruction: str) -> Optional[str]:
    text = instruction.lower()
    if _CLOCK_MINUTE_RE.search(instruction):
        return "minute"
    if _CLOCK_HOUR_RE.search(instruction):
        return "hour"
    if any(w in instruction for w in ("上午", "下午", "时段")) or "am" in text or "pm" in text:
        return "period"
    if "年" in instruction:
        return "year"
    if "月" in instruction:
        return "month"
    if "日" in instruction or "号" in instruction:
        return "day"
    return None


def _forward_steps(column: str, cur: int, tgt: int) -> int:
    if column == "minute":
        return (tgt - cur) % 60
    if column == "hour":
        return (tgt - cur) % 12
    return max(0, tgt - cur)


def _backward_steps(column: str, cur: int, tgt: int) -> int:
    if column == "minute":
        return (cur - tgt) % 60
    if column == "hour":
        return (cur - tgt) % 12
    return max(0, cur - tgt)


def _parse_picker_direction_and_steps(instruction: str, column: Optional[str]) -> tuple[Optional[str], Optional[int]]:
    """Best-effort fallback when planner hints are absent."""
    if not column:
        return None, None
    nums = [int(n) for n in re.findall(r"\d+", instruction)]
    if len(nums) < 2:
        return None, None
    cur, tgt = nums[0], nums[-1]
    if cur == tgt:
        return None, 0
    if column in {"minute", "hour"}:
        forward = _forward_steps(column, cur, tgt)
        backward = _backward_steps(column, cur, tgt)
        return ("increase" if forward <= backward else "decrease"), min(forward, backward)
    return ("increase" if tgt > cur else "decrease"), abs(tgt - cur)


def _picker_amount(column: str, steps: Optional[int]) -> str:
    if steps is not None and steps <= 0:
        return "small"

    # Period wheels are binary; always nudge one row.
    if column in {"period", "ampm"}:
        return "small"

    # Hour wheels wrap quickly. Keep 3-step moves small to preserve the 09->06
    # anti-oscillation regression, but allow longer moves to close faster.
    if column == "hour":
        return "medium" if steps is not None and steps >= 4 else "small"

    # Minute/date wheels need a coarse phase; otherwise 17->30 takes six or more
    # LLM turns. The executor maps picker large to a bounded, column-safe gesture.
    if steps is not None:
        if steps <= 2:
            return "small"
        if steps <= 7:
            return "medium"
        return "large"
    return "medium"


def _resolve_picker_hints(
    instruction: str,
    direction: Optional[str],
    drag_column: Optional[str],
    drag_steps: Optional[int],
) -> tuple[Optional[str], Optional[str], Optional[int], Optional[str]]:
    if _is_tap_only_instruction(instruction):
        return None, None, None, None

    column = drag_column or (_infer_picker_column(instruction) if _looks_like_picker_adjust(instruction) else None)
    value_direction = direction if direction in ("increase", "decrease") else None
    steps = drag_steps
    if column and (value_direction is None or steps is None):
        parsed_direction, parsed_steps = _parse_picker_direction_and_steps(instruction, column)
        value_direction = value_direction or parsed_direction
        steps = steps if steps is not None else parsed_steps
    if column and steps == 0:
        return value_direction, None, steps, None

    scroll_direction = _PICKER_VALUE_TO_SCROLL_DIRECTION.get(value_direction or "")
    return value_direction, column, steps, scroll_direction


def _prepare_android_png(png_bytes: bytes) -> bytes:
    """Vision-only image prep: send the raw screenshot, downscaled only if huge."""
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(png_bytes))
        longest = max(img.width, img.height)
        if longest <= _MAX_EDGE:
            return png_bytes
        scale = _MAX_EDGE / longest
        resized = img.resize(
            (max(1, int(img.width * scale)), max(1, int(img.height * scale))),
            Image.LANCZOS,
        )
        buf = io.BytesIO()
        resized.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        # Never block a decision on image prep; send raw bytes.
        return png_bytes


class AndroidActionPolicy(BaseActionPolicy):
    """Vision-based android action policy. Uses the shared BaseActionPolicy.decide()
    template; picker hints are injected and enforced for Android wheel pickers."""

    name = "android_vision"
    SYSTEM_PROMPT = SYSTEM_PROMPT
    decision_schema = AndroidActionDecision

    def _prepare_png(self, png_bytes: bytes) -> bytes:
        return _prepare_android_png(png_bytes)

    def _build_user_text(
        self,
        instruction: str,
        *,
        direction: Optional[str] = None,
        drag_column: Optional[str] = None,
        drag_steps: Optional[int] = None,
    ) -> str:
        value_direction, column, steps, scroll_direction = _resolve_picker_hints(
            instruction, direction, drag_column, drag_steps
        )
        hint_parts: list[str] = []
        if column:
            hint_parts.append(f"⚠️ Picker 列约束：只能滚动 {column} 列。")
        if value_direction:
            hint_parts.append(
                "⚠️ Picker 值方向约束："
                f"value_direction={value_direction}，Android scroll direction 必须是 {scroll_direction}。"
            )
        if steps is not None:
            hint_parts.append(f"⚠️ Picker 距离：约 {steps} 格，按距离选择 small/medium/large，近距离必须收小。")
        if hint_parts:
            hint_parts.append("⚠️ Picker 必须输出 action_type=scroll，禁止 tap/drag。")
        hint_prefix = "\n".join(hint_parts)
        return (
            f"{hint_prefix}\n操作指令：{instruction}\n\n请根据截图执行该指令。"
            if hint_prefix
            else f"操作指令：{instruction}\n\n请根据截图执行该指令。"
        )

    def _postprocess(
        self,
        decision: AndroidActionDecision,
        instruction: str,
        *,
        direction: Optional[str] = None,
        drag_column: Optional[str] = None,
        drag_steps: Optional[int] = None,
    ) -> AndroidActionDecision:
        _, column, steps, scroll_direction = _resolve_picker_hints(
            instruction, direction, drag_column, drag_steps
        )
        action = decision.action

        if column:
            anchor = _PICKER_COLUMN_ANCHORS.get(column)
            if anchor:
                if action.action_type != "scroll":
                    print(f"  [ActionPolicy] Android picker 强制 scroll（原 action={action.action_type}，column={column}）")
                    action.action_type = "scroll"
                action.x, action.y = anchor
                action.amount = _picker_amount(column, steps)
                action.snap = {**(action.snap or {}), "picker_column": column}
                if scroll_direction:
                    action.direction = scroll_direction
                elif action.direction not in ("up", "down"):
                    action.direction = "up"
            return decision

        if direction in ("up", "down", "left", "right") and action.action_type == "scroll":
            action.direction = direction
        return decision
