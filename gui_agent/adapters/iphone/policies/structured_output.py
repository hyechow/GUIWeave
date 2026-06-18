"""Structured-output multimodal LLM action policy."""

import re
from typing import Optional

from dotenv import load_dotenv

from gui_agent.adapters.iphone.actions import IPhoneActionDecision
from gui_agent.core.policies.base import BaseActionPolicy, resize_to_logical_png
from gui_agent.prompts import load_prompt_text

load_dotenv()


SYSTEM_PROMPT = load_prompt_text("task.action_policy.iphone")


# ⚠️ 只为 prompt 的「手指方向」提示把 value-direction(increase/decrease) 译成 up/down；
# 但传给 _normalize_drag_direction 的必须是**原始 direction**(increase/decrease)，让它设
# value_direction——gesture 层对 value_direction 的映射才是正确的。若把译后的 up/down 传进去，
# 会被当 finger 方向、再被 _drag_delta 反向解读(向上→数值变小)，picker 数值发散。
# increase = value grows = gesture 手指向上(to_y < y)；decrease = 手指向下。
_PICKER_VALUE_TO_GESTURE = {"increase": "up", "decrease": "down"}


def _resolve_hints(
    instruction: str,
    direction: Optional[str],
    drag_column: Optional[str],
    drag_steps: Optional[int],
) -> tuple[Optional[str], Optional[str], Optional[int], Optional[str]]:
    """Tap-only suppression + translate value-direction to a finger-gesture hint.

    Hints only apply to drag/scroll. If the instruction is clearly a tap (tap keywords,
    no drag/scroll keywords), suppress hints so the policy doesn't choose drag.
    Returns ``(direction, drag_column, drag_steps, gesture_hint_dir)``.
    """
    _drag_scroll_words = ("拖动", "拖拽", "滑动", "滚动", "drag", "scroll")
    _tap_only = (
        any(w in instruction for w in ("点击", "tap", "轻触"))
        and not any(w in instruction for w in _drag_scroll_words)
    )
    if _tap_only:
        direction = drag_column = None
        drag_steps = None
    gesture_hint_dir = _PICKER_VALUE_TO_GESTURE.get(direction or "", direction)
    return direction, drag_column, drag_steps, gesture_hint_dir


class StructuredOutputPolicy(BaseActionPolicy):
    """Vision-based iPhone action policy: LLM screenshot analysis + structured output
    + picker post-processing. Uses the shared BaseActionPolicy.decide() template;
    only the iphone-specific hooks live here."""

    name = "structured_output"
    SYSTEM_PROMPT = SYSTEM_PROMPT
    decision_schema = IPhoneActionDecision

    def _prepare_png(self, png_bytes: bytes) -> bytes:
        # iPhone mirror is 2x Retina; downsample to logical px for the vision model.
        return resize_to_logical_png(png_bytes)

    def _build_user_text(
        self,
        instruction: str,
        *,
        direction: Optional[str] = None,
        drag_column: Optional[str] = None,
        drag_steps: Optional[int] = None,
    ) -> str:
        direction, drag_column, _, gesture_hint_dir = _resolve_hints(
            instruction, direction, drag_column, drag_steps
        )
        hint_parts: list[str] = []
        if gesture_hint_dir:
            dir_zh = {"up": "上", "down": "下", "left": "左", "right": "右"}.get(gesture_hint_dir, gesture_hint_dir)
            hint_parts.append(f"⚠️ 方向约束：手指必须向{dir_zh}移动（{gesture_hint_dir}）。")
        if drag_column:
            hint_parts.append(f"⚠️ 目标列：{drag_column}。")
        hint_prefix = "\n".join(hint_parts)
        return (
            f"{hint_prefix}\n操作指令：{instruction}\n\n请根据截图执行该指令。"
            if hint_prefix
            else f"操作指令：{instruction}\n\n请根据截图执行该指令。"
        )

    def _postprocess(
        self,
        decision: IPhoneActionDecision,
        instruction: str,
        *,
        direction: Optional[str] = None,
        drag_column: Optional[str] = None,
        drag_steps: Optional[int] = None,
    ) -> IPhoneActionDecision:
        direction, drag_column, drag_steps, gesture_hint_dir = _resolve_hints(
            instruction, direction, drag_column, drag_steps
        )
        _normalize_drag_direction(decision, instruction, direction, drag_steps)
        _normalize_scroll_direction(decision, gesture_hint_dir)
        _force_picker_column(decision, drag_column)
        _fix_date_range_field_mixup(decision, instruction)
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
    decision: IPhoneActionDecision,
    instruction: str,
    direction_hint: Optional[str] = None,
    step_count: Optional[int] = None,
) -> None:
    """Normalize semantic drag direction and amount.

    step_count（来自 planner 的结构化 drag_steps）优先用于按距离选幅度；缺省时退回
    从指令文本正则抠 |当前-目标| 的旧路径。差≥4 格用 large 粗调、2-3 格 medium、≤1 格 small。
    """
    action = decision.action
    if action.action_type != "drag":
        return
    # ⚠️ planner 的结构化 direction_hint（increase/decrease）必须优先生效——它是经
    # _fix_picker_direction 校正后的权威方向。此前本块因缩进错误被放在 return 之后成了死代码，
    # 导致 hint 完全没应用、落到下方关键词分支把「向上」当 finger 方向，再被 gesture 反向解读
    # （向上→数值变小），造成 picker 数值朝反方向无限发散（如年份 2026→2016）。
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

    # 选幅度：优先用结构化 step_count（格数）；缺省时回退到从指令文本正则抠出的距离。
    # 阈值与历史一致：1 格→small（近距离精调）、≥4 格→large（远距离粗调）；2-3 格不强制、
    # 保留 LLM 给的 amount。本修复只为补上「距离根本读不出 → 远距离也退化成 small」这个洞，
    # 不改 2-3 格的既有行为。
    steps = step_count
    if steps is None:
        step_distance = _picker_step_distance(instruction)
        steps = round(step_distance / _PICKER_ROW_NORM) if step_distance else None
    if steps is not None:
        if steps <= 1:
            action.amount = "small"
        elif steps >= 4:
            action.amount = "large"


_DRAG_COLUMN_TO_AREA = {"year": "picker_left", "month": "picker_center", "day": "picker_right"}


def _force_picker_column(decision: IPhoneActionDecision, drag_column: Optional[str]) -> None:
    """用 planner 结构化算出的 drag_column 硬覆盖 picker 列，不信任 action_policy 从指令文本
    解析出的列。

    根因：列此前完全由不可靠的自由指令文本决定——planner 偶尔把月份误写成「年份列」(reasoning
    /drag_column 其实是 month)，action_policy 跟文本选了年份列 → 拖错列、把已对齐的年份拖坏。
    drag_column 是经规则校正的权威列，year/month/day 直接映射到校准好的 picker_left/center/right，
    并清空文本估计的 x，改用该列的校准坐标。
    """
    if not drag_column:
        return
    area = _DRAG_COLUMN_TO_AREA.get(drag_column)
    if not area:
        return
    action = decision.action
    if action.action_type != "drag":
        return
    if action.target_area != area or action.x is not None:
        print(f"  [ActionPolicy] picker 列校正：{action.target_area}→{area}（按 drag_column={drag_column}）")
        action.target_area = area
        action.x = None  # 用 picker_* 的校准列坐标，弃用文本估计的 x


def _normalize_scroll_direction(decision: IPhoneActionDecision, direction_hint: Optional[str]) -> None:
    """Override scroll direction with structured hint."""
    if direction_hint not in ("up", "down", "left", "right"):
        return
    action = decision.action
    if action.action_type != "scroll":
        return
    action.direction = direction_hint


def _fix_date_range_field_mixup(decision: IPhoneActionDecision, instruction: str) -> None:
    """Guard against LLM confusing 开始时间 (left) and 结束时间 (right).

    The action policy model consistently misidentifies the two fields on the
    screenshot.  When the instruction says 开始时间 but the model outputs
    结束时间 (or vice versa), swap both description and coordinates.
    """
    action = decision.action
    if action.action_type not in ("tap", "click") or not action.description:
        return

    inst_has_start = "开始时间" in instruction or "开始日期" in instruction
    inst_has_end = "结束时间" in instruction or "结束日期" in instruction
    desc_has_start = "开始时间" in action.description or "开始日期" in action.description
    desc_has_end = "结束时间" in action.description or "结束日期" in action.description

    if inst_has_start and not inst_has_end and desc_has_end and not desc_has_start:
        action.description = action.description.replace("结束时间", "开始时间").replace("结束日期", "开始日期")
        if action.x is not None and action.x > 500:
            action.x = 1000 - action.x
            print(f"  ⚠️ 日期范围字段混淆：开始→结束 已自动纠正，x {1000 - action.x + action.x:.0f}→{action.x:.0f}")
    elif inst_has_end and not inst_has_start and desc_has_start and not desc_has_end:
        action.description = action.description.replace("开始时间", "结束时间").replace("开始日期", "结束日期")
        if action.x is not None and action.x < 500:
            old_x = action.x
            action.x = 1000 - action.x
            print(f"  ⚠️ 日期范围字段混淆：结束→开始 已自动纠正，x {old_x:.0f}→{action.x:.0f}")
