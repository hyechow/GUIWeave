"""Vision-only Android action policy: screenshot + instruction -> one Action.

Mirrors the iphone ``StructuredOutputPolicy`` LLM machinery — same config via
``resolve_llm_config('action_policy')``, same structured-output call via
``invoke_structured`` into an ``ActionDecision``, same ``BaseActionPolicy`` base —
but with an ANDROID system prompt: operate a phone touchscreen, output ONE action
within the android action vocabulary (tap / type / clear_text / press_enter /
scroll / drag / home / back / app_switch / stop).

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

from gui_agent.core.policies.base import BaseActionPolicy
from gui_agent.adapters.android.actions import AndroidActionDecision

load_dotenv()


SYSTEM_PROMPT = """\
你是一个 Android 手机操作执行器。
用户会提供 Android 手机截图和一个具体的操作指令。你只需要找到目标元素并输出对应的单个动作。

坐标使用归一化坐标系：截图左上角为 (0,0)，右下角为 (1000,1000)，覆盖整个手机屏幕。

可用动作（只能从中选一个）：
- tap：点击应用图标、按钮、菜单项、列表项、开关、底部导航栏标签等可点击元素。填写目标中心的 x/y。
- type：在输入框中输入任意文字（含中文）。填写输入框中心的 x/y 和 text，它会自动先点击聚焦、清空原有内容、再输入。
  只有当指令明确说明输入框已聚焦时，type 才可以只填 text、不填 x/y。
- press_enter：提交 / 确认 / 搜索 / 换行。输入文字后需要提交时使用，无需坐标。禁止用 tap 点发送或搜索按钮来代替回车提交。
- clear_text：清空当前聚焦输入框的内容，无需坐标。
- scroll：滚动列表或页面以显示更多内容。填写 direction（down 看下方、up 看上方、left 看左侧、right 看右侧）、amount（small/medium/large）；
  局部滚动容器需填写 x/y 作为滚动锚点，落在要滚动的区域内。
- drag：拖动滑块、进度条等需要拖拽的控件。填写起点 x/y。
- home：回到手机主屏幕（等价于系统主屏键），无需坐标。
- back：系统返回键，返回上一级 / 关闭当前弹窗或页面，无需坐标。
- app_switch：打开 App 切换器 / 最近任务（多任务视图），随后可 tap 卡片切换 App，无需坐标。
- stop：当指令含义是「停止」「无需操作」「目标已完成」，或目标元素确实不在当前截图中时使用，无需坐标。

Android 操作约定：
- 「返回上一级」优先用 back（系统返回键），或点击界面内的返回按钮（通常在左上角，形如 ← 箭头）/ 底部导航栏对应标签；只有明确需要退出当前应用回到桌面时才用 home。
- 屏幕顶部是状态栏 / 通知栏，底部常有导航栏（多个标签 tab）。应用列表 / 抽屉中的图标用 tap 打开。
- 输入文字后软键盘会从屏幕下半部分弹出并遮挡内容；输入完成后用 press_enter 提交并收起键盘。
- amount 表示滚动幅度：small（细微调整）、medium（普通翻看）、large（快速翻页）。普通整页滚动可不填 x/y；局部容器 / 分栏滚动必须填 x/y 落在该区域中心。
- 不要填写 to_x/to_y/duration_ms（drag 只需给出起点）。
- description 用中文简要说明操作目标，必须与指令中的目标元素名称一致。

## 滚轮选择器（字段枚举 picker）
- 形态：一列或多列上下滚动的轮子；每列是一个字段，每列中间高亮的那一行就是该字段当前枚举值。字段可能是时间、日期、地区、重复规则、铃声、类别、颜色、尺寸、数量等任意含义，不要默认只有时间/日期。
- 改值只能靠 scroll，**分两段**：离目标还差很多格时用 amount=medium/large 快速接近；差几格时改用 amount=small 一格格精调，把目标字段值停到中间高亮行。别一直用 small（远距离挪不到），也别一直用 medium（近了会冲过头）；冲过头就反向滚回来。
- 当操作指令或附加提示明确给出 picker 的方向/列/步数时，必须服从提示：不要自行改方向、不要改列、不要输出 tap。
- scroll 的 x/y 锚点必须落在要滚的字段列上，不要落在屏幕最上方或最下方。
- **绝不要 tap picker**（包括点中间那个已选中的枚举值）——picker 的值只靠滚动设定，点击既不生效、也不需要点击来「确认」；要选某个值，只能把它滚到中间高亮行。

## 目标元素不可见时的处理
如果仔细检查截图后发现指令要求操作的元素确实不在当前可见区域：
- 如果可以通过滚动显示出来，输出 scroll。
- 如果确实不在当前页面，将 not_found_reason 填写为具体原因（如「当前页面无该按钮，可见的有 A、B、C」），
  action 使用 stop，description 说明找不到目标。
"""

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


def _is_tap_only_instruction(instruction: str) -> bool:
    return (
        any(w in instruction for w in ("点击", "tap", "轻触"))
        and not any(w in instruction for w in ("拖动", "拖拽", "滑动", "滚动", "调整", "调到", "调至"))
    )


def _looks_like_picker_adjust(instruction: str) -> bool:
    if any(w in instruction for w in _PICKER_WORDS):
        return True
    has_time_column = any(w in instruction for w in ("小时", "分钟", "上午", "下午", "点", "分"))
    return has_time_column and any(w in instruction for w in _PICKER_ADJUST_WORDS)


def _infer_picker_column(instruction: str) -> Optional[str]:
    text = instruction.lower()
    if any(w in instruction for w in ("分钟", "分")) or "minute" in text:
        return "minute"
    if any(w in instruction for w in ("小时", "点")) or "hour" in text:
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
