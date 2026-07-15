from gui_agent.prompts import load_prompt_text

# Checker prompt is assembled per milestone.kind: SINGLE_CHECKER_PROMPT is the
# shared core with a {kind_section} slot; CHECK_KIND_SECTIONS supplies only the
# rules relevant to the current kind. This keeps each checker focused (sharper
# done-judgment, less hallucination) and its output terse (faster). See
# model_io.run_checker for the dispatch.
SINGLE_CHECKER_PROMPT = load_prompt_text("task.milestone.iphone.checker")

# ── Per-kind checker sections (only the relevant one is injected) ──────────
_CHECK_SECTION_NAVIGATION = load_prompt_text("context.milestone.iphone.check.navigation")

_CHECK_SECTION_FILTER = load_prompt_text("context.milestone.iphone.check.filter")

_CHECK_SECTION_ACTION = load_prompt_text("context.milestone.iphone.check.action")

_CHECK_SECTION_COLLECTION = load_prompt_text("context.milestone.iphone.check.collection")

# 连续调值类（completion_strategy=repeat_until_satisfied）专用补充段。叠加在 kind 段之上，
# 由 run_checker 在 milestone.is_converge 时追加。核心：当前值以滚轮中心带为准（摘要框滞后），
# 并强制输出当前值/目标值，作为连续操作的进展传感器（供 planner 算步长、供 stuck 判值停滞）。
_CHECK_SECTION_CONVERGE = load_prompt_text("context.milestone.iphone.check.converge")

# Unknown/other kinds fall back to all sections (same coverage as before split).
_CHECK_SECTION_DEFAULT = (
    _CHECK_SECTION_NAVIGATION + _CHECK_SECTION_FILTER
    + _CHECK_SECTION_ACTION + _CHECK_SECTION_COLLECTION
)

CHECK_KIND_SECTIONS = {
    "navigation": _CHECK_SECTION_NAVIGATION,
    "filter": _CHECK_SECTION_FILTER,
    "action": _CHECK_SECTION_ACTION,
    "collection": _CHECK_SECTION_COLLECTION,
    "verification": _CHECK_SECTION_COLLECTION,  # banned by decomposer, treat as read
}

LOOP_FRAME_PROMPT = load_prompt_text("task.milestone.iphone.loop_frame")

PLAN_PROMPT = load_prompt_text("task.milestone.iphone.planner")

LOOP_SCROLL_PROMPT = load_prompt_text("task.milestone.iphone.loop_scroll")

REPLAN_PROMPT = load_prompt_text("task.milestone.iphone.replanner")

# ── Bundle the above into the neutral MilestonePrompts seam ──────────────────
# This iPhone-flavored bundle owns the injection boundary; the model-visible
# prompt bodies live under gui_agent/prompts/tasks/milestone/iphone/.
from gui_agent.core.supervisor.milestone.schemas import MilestonePrompts  # noqa: E402

IPHONE_MILESTONE_PROMPTS = MilestonePrompts(
    single_checker=SINGLE_CHECKER_PROMPT,
    check_kind_sections=CHECK_KIND_SECTIONS,
    check_section_default=_CHECK_SECTION_DEFAULT,
    check_section_converge=_CHECK_SECTION_CONVERGE,
    loop_frame=LOOP_FRAME_PROMPT,
    plan=PLAN_PROMPT,
    loop_scroll=LOOP_SCROLL_PROMPT,
    replan=REPLAN_PROMPT,
    home_identity_markers=("iOS 主屏幕", "主屏幕", "主屏", "home screen", "springboard"),
)
