"""Structured-output multimodal LLM action policy."""

import base64
from typing import Optional

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from policy_expr.config import resolve_llm_config
from llm.structured import invoke_structured
from policy_expr.policies.base import BaseActionPolicy, resize_to_logical_png
from policy_expr.schemas import ActionDecision, Observation

load_dotenv()


SYSTEM_PROMPT = """\
你是一个 iPhone 操作执行器。
用户会提供 iPhone 截图和一个具体的操作指令。你只需要找到目标元素并输出对应动作。

坐标使用归一化坐标系：左上角(0,0)，右下角(1000,1000)。
需要在输入框中输入文字时，使用 type（而非 tap），并填写输入框中心的 x/y 坐标和 text，它会自动先点击再输入。
只有当操作指令明确说明输入框已经聚焦时，type 可以只填写 text、不填写 x/y。
⚠️ 输入文字后如果需要发送/提交/确认（如发送消息、确认搜索），必须使用 press_enter，无需填写坐标。禁止用 tap 去点击发送按钮。
需要清空当前输入框内容时，使用 clear_text，无需填写坐标。
需要滚动普通列表页面时，使用 scroll，填写 direction 和 x/y 坐标。
- down：向下滚动，查看页面下方的内容
- up：向上滚动，查看页面上方的内容
- left：向左滚动，查看右侧内容（翻到下一页）
- right：向右滚动，查看左侧内容（翻到上一页）
多数按时间倒序的列表页新内容在顶部，要查看更早/更旧的内容通常选择 down；若截图显示相反方向，应以当前 UI 结构为准。
x/y 是滚轮事件的鼠标落点坐标，决定了滚轮作用于哪个 UI 元素：
- 整页滚动：x/y 放在可滚动内容的中部
y 坐标范围严格限制在 200-850 之间，禁止使用 y<200 或 y>850
滚轮选择器/日期选择器/时间选择器/城市选择器等多列 picker，不要使用 scroll，必须使用 drag（按住拖动）。
- drag 需要填写起点 x/y、终点 to_x/to_y、duration_ms
- x/to_x 必须落在目标列中心：年份列偏左、月份列偏右，日期列按截图位置选择
- y 放在当前选中行或目标列可拖动区域中间；to_y 表示拖动后的终点
- 如果用户消息中有「⚠️ 方向约束」，to_y 方向必须遵守该约束，不以指令文字为准：down → to_y > y，up → to_y < y
- 如果用户消息中有「⚠️ 拖动幅度」，按幅度控制 |to_y - y|：小幅约70，中幅约130，大幅约200
- duration_ms 参考：小幅=800ms，中幅=1200ms，大幅=1600ms
- 例如要把年份列往下拉（大幅）：action_type=drag, x=270, y=635, to_x=270, to_y=835, duration_ms=1600
需要返回主屏幕时，使用 home，无需填写坐标。
⚠️ home 只用于「明确需要退出当前应用回到桌面」的场景。如果目标元素在当前页面不可见，应优先寻找应用内的导航路径（如左上角返回按钮、底部 tab），而不是直接 home。
如果指令含义是「停止操作」「无需操作」「目标已完成」，使用 stop，无需填写任何坐标或文字。
action 的 description 用中文简要说明操作目标即可。

## 目标元素不可见时的处理
如果仔细检查截图后发现指令要求操作的 UI 元素确实不在当前屏幕上，不要猜测坐标或执行其他操作。
此时将 not_found_reason 填写为具体原因（如「当前页面无目标Tab，可见标签为A、B、C」），
action 使用 stop，description 说明找不到目标。
"""


class StructuredOutputPolicy(BaseActionPolicy):
    """Vision-based action policy: LLM screenshot analysis + structured output."""

    name = "structured_output"

    def decide(
        self,
        observation: Observation,
        instruction: str,
        *,
        direction: Optional[str] = None,
        drag_column: Optional[str] = None,
        drag_magnitude: Optional[str] = None,
        verbose: bool = True,
    ) -> ActionDecision:
        cfg = resolve_llm_config("action_policy")
        if verbose:
            print(f"Provider : {cfg.provider}")
            print(f"Model    : {cfg.model}")

        b64 = base64.b64encode(resize_to_logical_png(observation.png_bytes)).decode()
        llm = ChatOpenAI(
            model=cfg.model,
            api_key=cfg.api_key,
            base_url=cfg.base_url,
        )

        # Hints only apply to drag/scroll actions. If the instruction is clearly a tap
        # (contains tap keywords and no drag/scroll keywords), suppress hints to avoid
        # the action policy choosing drag when it should tap.
        _drag_scroll_words = ("拖动", "拖拽", "滑动", "滚动", "drag", "scroll")
        _tap_only = (
            any(w in instruction for w in ("点击", "tap", "轻触"))
            and not any(w in instruction for w in _drag_scroll_words)
        )
        if _tap_only:
            direction = drag_column = drag_magnitude = None

        # Translate picker value-direction (increase/decrease) to gesture direction.
        # increase = value grows = gesture up (to_y < y)
        # decrease = value shrinks = gesture down (to_y > y)
        _PICKER_VALUE_TO_GESTURE = {"increase": "up", "decrease": "down"}
        if direction in _PICKER_VALUE_TO_GESTURE:
            direction = _PICKER_VALUE_TO_GESTURE[direction]

        hint_parts: list[str] = []
        if direction:
            dir_zh = {"up": "上", "down": "下", "left": "左", "right": "右"}.get(direction, direction)
            hint_parts.append(f"⚠️ 方向约束：手指必须向{dir_zh}移动（{direction}）。")
        if drag_column:
            hint_parts.append(f"⚠️ 目标列：{drag_column}。")
        if drag_magnitude:
            mag_zh = {"small": "小幅（约1格）", "medium": "中幅（约2-3格）", "large": "大幅（4格以上）"}.get(drag_magnitude, drag_magnitude)
            hint_parts.append(f"⚠️ 拖动幅度：{mag_zh}。")
        hint_prefix = "\n".join(hint_parts)
        user_text = f"{hint_prefix}\n操作指令：{instruction}\n\n请根据截图执行该指令。" if hint_prefix else f"操作指令：{instruction}\n\n请根据截图执行该指令。"

        messages = [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(
                content=[
                    {"type": "text", "text": user_text},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"},
                    },
                ]
            ),
        ]
        decision = invoke_structured(llm, messages, ActionDecision)
        _normalize_drag_direction(decision, instruction, direction, drag_magnitude)
        _normalize_scroll_direction(decision, direction)
        return decision


_MAGNITUDE_DELTA: dict[str, int] = {"small": 70, "medium": 140, "large": 300}


def _normalize_drag_direction(
    decision: ActionDecision,
    instruction: str,
    direction_hint: Optional[str] = None,
    magnitude_hint: Optional[str] = None,
) -> None:
    """Enforce drag direction and magnitude from structured hints, falling back to keyword parsing."""
    action = decision.action
    if action.action_type != "drag":
        return
    if action.y is None or action.to_y is None:
        return

    if direction_hint in ("up", "down"):
        wants_down = direction_hint == "down"
        wants_up = direction_hint == "up"
    else:
        wants_down = any(word in instruction for word in ("向下", "下拉", "往下"))
        wants_up = any(word in instruction for word in ("向上", "上滑", "上拉", "往上"))

    distance = _MAGNITUDE_DELTA.get(magnitude_hint or "", abs(action.to_y - action.y) or 200)

    if wants_down and wants_up:
        # No direction determined — only apply magnitude if hint exists
        if magnitude_hint in _MAGNITUDE_DELTA:
            if action.to_y > action.y:
                action.to_y = min(850, action.y + distance)
            else:
                action.to_y = max(200, action.y - distance)
        return

    if wants_down:
        action.to_y = min(850, action.y + distance)
    elif wants_up:
        action.to_y = max(200, action.y - distance)


def _normalize_scroll_direction(decision: ActionDecision, direction_hint: Optional[str]) -> None:
    """Override scroll direction with structured hint."""
    if direction_hint not in ("up", "down", "left", "right"):
        return
    action = decision.action
    if action.action_type != "scroll":
        return
    action.direction = direction_hint
