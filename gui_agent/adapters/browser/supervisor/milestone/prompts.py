"""Browser milestone supervisor prompts."""

from gui_agent.prompts import load_prompt_text

DECOMPOSE_PROMPT = load_prompt_text("task.milestone.browser.decompose")

SINGLE_CHECKER_PROMPT = load_prompt_text("task.milestone.browser.checker")

# ── Per-kind checker sections (only the relevant one is injected) ──────────
_CHECK_SECTION_NAVIGATION = load_prompt_text("context.milestone.browser.check.navigation")

_CHECK_SECTION_FILTER = load_prompt_text("context.milestone.browser.check.filter")

_CHECK_SECTION_ACTION = load_prompt_text("context.milestone.browser.check.action")

_CHECK_SECTION_COLLECTION = load_prompt_text("context.milestone.browser.check.collection")

_CHECK_SECTION_CONVERGE = load_prompt_text("context.milestone.browser.check.converge")

_CHECK_SECTION_DEFAULT = (
    _CHECK_SECTION_NAVIGATION + _CHECK_SECTION_FILTER
    + _CHECK_SECTION_ACTION + _CHECK_SECTION_COLLECTION
)

CHECK_KIND_SECTIONS = {
    "navigation": _CHECK_SECTION_NAVIGATION,
    "filter": _CHECK_SECTION_FILTER,
    "action": _CHECK_SECTION_ACTION,
    "collection": _CHECK_SECTION_COLLECTION,
    "verification": _CHECK_SECTION_COLLECTION,
}

LOOP_FRAME_PROMPT = load_prompt_text("task.milestone.browser.loop_frame")

PLAN_PROMPT = load_prompt_text("task.milestone.browser.planner")

LOOP_SCROLL_PROMPT = load_prompt_text("task.milestone.browser.loop_scroll")

REPLAN_PROMPT = load_prompt_text("task.milestone.browser.replanner")

STOP_CONDITION_PATCH_PROMPT = load_prompt_text("task.milestone.browser.stop_condition_patch")


# ── Browser-specific structured planner output ──────────────────────────────
from typing import Literal, Optional  # noqa: E402

from pydantic import BaseModel, Field  # noqa: E402


class BrowserPlanResult(BaseModel):
    instruction: str = Field(description="下一步精确操作指令；输入/选择具名值时必须包含子目标要求的目标原文")
    summary: str = Field(description="规划依据一句话摘要")
    atomic_role: Literal["prepare", "write", "commit", "iterate"] = Field(
        default="prepare",
        description="prepare=展开/定位；write=填写/选择目标值；commit=保存/提交；iterate=滚动/拖动。",
    )
    action_family: Literal[
        "input", "select", "activate", "navigate", "iterate", "commit", "unknown"
    ] = Field(
        default="unknown",
        description=(
            "指令要求的原子动作族；必须按指令本身填写，不能按猜测的 action-policy 输出填写。"
        ),
    )
    target_control: str = Field(
        default="",
        description="本轮动作实际要命中的控件或字段名称；必须与子目标声明目标一致。",
    )
    direction: Optional[Literal["up", "down", "left", "right"]] = Field(
        default=None,
        description="只有下一步需要滚动时填写：down=查看下方内容，up=查看上方内容，left/right=横向查看内容；其他操作留空",
    )


# ── Bundle into the neutral MilestonePrompts seam (web draft) ────────────────
from gui_agent.core.supervisor.milestone.schemas import MilestonePrompts  # noqa: E402

BROWSER_MILESTONE_PROMPTS = MilestonePrompts(
    decompose=DECOMPOSE_PROMPT,
    single_checker=SINGLE_CHECKER_PROMPT,
    check_kind_sections=CHECK_KIND_SECTIONS,
    check_section_default=_CHECK_SECTION_DEFAULT,
    check_section_converge=_CHECK_SECTION_CONVERGE,
    loop_frame=LOOP_FRAME_PROMPT,
    plan=PLAN_PROMPT,
    loop_scroll=LOOP_SCROLL_PROMPT,
    replan=REPLAN_PROMPT,
    stop_condition_patch=STOP_CONDITION_PATCH_PROMPT,
    image_resize="none",
    plan_result_schema=BrowserPlanResult,
)
