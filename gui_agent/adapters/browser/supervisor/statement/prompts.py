"""Browser statement supervisor prompts."""

from gui_agent.prompts import load_prompt_text

SINGLE_CHECKER_PROMPT = load_prompt_text("task.statement.browser.checker")

# ── Per-kind checker sections (only the relevant one is injected) ──────────
_CHECK_SECTION_NAVIGATION = load_prompt_text("context.statement.browser.check.navigation")

_CHECK_SECTION_FILTER = load_prompt_text("context.statement.browser.check.filter")

_CHECK_SECTION_ACTION = load_prompt_text("context.statement.browser.check.action")

_CHECK_SECTION_COLLECTION = load_prompt_text("context.statement.browser.check.collection")

_CHECK_SECTION_CONVERGE = load_prompt_text("context.statement.browser.check.converge")

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

LOOP_FRAME_PROMPT = load_prompt_text("task.statement.browser.loop_frame")

PLAN_PROMPT = load_prompt_text("task.statement.browser.planner")

LOOP_SCROLL_PROMPT = load_prompt_text("task.statement.browser.loop_scroll")

REPLAN_PROMPT = load_prompt_text("task.statement.browser.replanner")

# ── Browser-specific structured planner output ──────────────────────────────
from typing import Literal, Optional  # noqa: E402

from pydantic import BaseModel, Field, field_validator  # noqa: E402

from gui_agent.core.supervisor.statement.schemas import StatementPrompts  # noqa: E402


class BrowserPlanResult(BaseModel):
    instruction: str = Field(description="下一步精确操作指令；输入/选择具名值时必须包含子目标要求的目标原文")
    summary: str = Field(description="规划依据一句话摘要")
    atomic_role: Literal["prepare", "write", "commit", "iterate"] = Field(
        default="prepare",
        description=(
            "prepare=展开/定位；write=填写/选择目标值；commit=保存/提交；iterate=滚动/拖动。"
            "不要填 navigate/activate/input/select——那些属于 action_family。"
        ),
    )
    action_family: Literal[
        "input", "select", "activate", "navigate", "iterate", "unknown"
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
    target_value: str = Field(
        default="",
        description="本轮写入或选择的结构化目标值。",
    )
    direction: Optional[Literal["up", "down", "left", "right"]] = Field(
        default=None,
        description="只有下一步需要滚动时填写：down=查看下方内容，up=查看上方内容，left/right=横向查看内容；其他操作留空",
    )

    @field_validator("target_control", "target_value", mode="before")
    @classmethod
    def _coerce_optional_str(cls, v):
        # DashScope json_object occasionally emits ``null`` for an optional string field
        # even though it has an empty-string default. An explicit null fails the primary
        # model_validate ("None is not a valid str") — the default does not apply because
        # the key is present — and invoke_structured then falls back to a slow plain-text
        # JSON reparse. Treat null as the absent/empty default; stringify a scalar number
        # the model may emit for target_value (price/quantity). Required strings
        # (instruction, summary) are deliberately NOT coerced: a null there is a real
        # planner failure and the fallback reparse is the correct response.
        if v is None:
            return ""
        if isinstance(v, (int, float)):
            return str(v)
        return v

# ── Bundle into the neutral StatementPrompts seam (web draft) ────────────────
BROWSER_STATEMENT_PROMPTS = StatementPrompts(
    single_checker=SINGLE_CHECKER_PROMPT,
    check_kind_sections=CHECK_KIND_SECTIONS,
    check_section_default=_CHECK_SECTION_DEFAULT,
    check_section_converge=_CHECK_SECTION_CONVERGE,
    loop_frame=LOOP_FRAME_PROMPT,
    plan=PLAN_PROMPT,
    loop_scroll=LOOP_SCROLL_PROMPT,
    replan=REPLAN_PROMPT,
    image_resize="none",
    plan_result_schema=BrowserPlanResult,
)
