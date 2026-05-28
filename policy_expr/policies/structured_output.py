"""Structured-output multimodal LLM action policy."""

import base64
import re
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
需要滚动普通列表页面时，使用 scroll，填写 direction、target_area、amount、method；必要时填写 x/y 作为滚动锚点。
- down：向下滚动，查看页面下方的内容
- up：向上滚动，查看页面上方的内容
- left：向左滚动，查看右侧内容（翻到下一页）
- right：向右滚动，查看左侧内容（翻到上一页）
多数按时间倒序的列表页新内容在顶部，要查看更早/更旧的内容通常选择 down；若截图显示相反方向，应以当前 UI 结构为准。
target_area 表示默认滚动作用区域：
- main_content：主内容区/整页列表
- left_panel/right_panel：左右分栏列表
- top_content/bottom_content：屏幕上方/下方内容区域
- picker_left/picker_center/picker_right：选择器左/中/右列
amount 表示幅度：small（细微调整）、medium（普通翻看）、large（快速翻页）。
method 表示执行方式：auto（运行时自动探测）、wheel（滚轮）、drag（触摸拖动）。
普通页面滚动使用 method=auto；滚轮选择器/日期选择器/时间选择器/城市选择器等多列 picker 使用 drag，并选择对应 picker_* target_area。
picker 调整数值时优先填写 value_direction，不要用 direction 表达数值变化：
- value_direction=increase：调大数值，例如 2025年→2026年、1月→2月、1日→2日
- value_direction=decrease：调小数值，例如 2026年→2025年、5月→4月、2日→1日
⚠️ Picker 数值调整强规则：
- 只要指令要求「把年份/月/日期/小时/分钟调大/调小」「上一年/下一年/上一月/下一月」「移动一格」，必须输出 action_type=drag，禁止输出 tap。
- 不要点击当前选中的年份/月/日文本；点击不会改变 picker 数值。
- 不要点击顶部绿色/高亮的已选日期展示框；那只是当前值展示，不是调值动作。
- 根据要调整的列选择 target_area：年份/小时/省份等左列用 picker_left；月份/分钟/城市等中列用 picker_center；日期/秒/区县等右列用 picker_right。
- amount 一格/一步用 small；多格或大幅调整才用 medium/large。具体阈值：差1-2格用 small，差3-6格用 medium，差7格及以上用 large。必须根据指令中当前值和目标值的差值选择 amount，不要一律输出 small。
- method 必须是 drag；普通 picker 调值不要用 auto 或 wheel。
- picker 调值通常不要填写 x/y；只用 target_area 表示列。只有当 picker 列不是常见左/中/右布局时，才填写 x 作为列锚点；不要填写 y。
示例：
- 指令「把年份调小一格」→ action_type=drag, target_area=picker_left, value_direction=decrease, amount=small, method=drag
- 指令「把月份调大一格」→ action_type=drag, target_area=picker_center, value_direction=increase, amount=small, method=drag
- 指令「把日期调小一格」→ action_type=drag, target_area=picker_right, value_direction=decrease, amount=small, method=drag
scroll/drag 的 x/y 是「滚动锚点」：
- 普通整页滚动可不填 x/y，执行层使用 target_area 默认点。
- 局部滚动容器、picker 多列、左右分栏必须填写 x/y，落在要滚动的容器或列中心。
- 不要填写 to_x/to_y/duration_ms；这些由执行层根据 direction/amount/method 自动计算。
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
            direction = drag_column = None

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
        _normalize_drag_direction(decision, instruction, direction)
        _normalize_scroll_direction(decision, direction)
        return decision


# One picker row in normalized (0-1000) units: row_height_pt / device_height_pt × 1000
# iPhone 16 Pro Max: 44pt row / 932pt screen × 1000 ≈ 47
_PICKER_ROW_NORM: int = 47


def _picker_step_distance(instruction: str) -> Optional[int]:
    """Compute drag distance for picker adjustments by counting steps in instruction.

    Parses patterns like '从18日调整至24日', '将当前选中的28日切换至8日', '从5月到1月'.
    Returns |cur - tgt| * row_height, or None for non-picker instructions.
    """
    for suffix in ("日", "月", "年"):
        # Pattern 1: "从X日至Y日" / "从X月调整至Y月"
        m = re.search(
            rf"从\s*(?:\d+[月年])?(\d+){suffix}.*?[至到调].*?(?:\d+[月年])?(\d+){suffix}",
            instruction,
        )
        if not m:
            # Pattern 2: "选中的X日...切换/调整至Y日" / "X日...至Y日"
            m = re.search(
                rf"(\d+){suffix}.*?[至到切换调].*?(\d+){suffix}",
                instruction,
            )
        if m:
            steps = abs(int(m.group(2)) - int(m.group(1)))
            if steps > 0:
                return steps * _PICKER_ROW_NORM
    return None


def _normalize_drag_direction(
    decision: ActionDecision,
    instruction: str,
    direction_hint: Optional[str] = None,
) -> None:
    """Normalize semantic drag direction and amount."""
    action = decision.action
    if action.action_type != "drag":
        return

        if direction_hint in ("increase", "decrease"):
            action.value_direction = direction_hint
        elif direction_hint in ("up", "down"):
            action.direction = direction_hint
    elif not action.value_direction:
        if any(word in instruction for word in ("调大", "增加", "增大", "往后", "下一")):
            action.value_direction = "increase"
        elif any(word in instruction for word in ("调小", "减少", "减小", "往前", "上一")):
            action.value_direction = "decrease"
        elif any(word in instruction for word in ("向下", "下滑", "往下")):
            action.direction = "down"
        elif any(word in instruction for word in ("向上", "上滑", "往上")):
            action.direction = "up"

    step_distance = _picker_step_distance(instruction)
    if step_distance and step_distance <= _PICKER_ROW_NORM:
        action.amount = "small"
    elif step_distance and step_distance >= _PICKER_ROW_NORM * 4:
        action.amount = "large"


def _normalize_scroll_direction(decision: ActionDecision, direction_hint: Optional[str]) -> None:
    """Override scroll direction with structured hint."""
    if direction_hint not in ("up", "down", "left", "right"):
        return
    action = decision.action
    if action.action_type != "scroll":
        return
    action.direction = direction_hint
