"""Android milestone-supervisor prompts — mobile-tuned DRAFT.

A first Android-tuned draft of the milestone supervisor prompts, parallel to the
iphone / browser sets. The supervisor FRAMEWORK is neutral; these prompts inject
Android concepts (App + 界面身份 / 顶部标题栏 / 底部导航 tab / 应用抽屉 / 三大金刚键
/ 软键盘) instead of iphone (iOS 主屏 / picker) or web (URL / 标签页 / navigate) ones.
The .format() placeholders are IDENTICAL to the iphone/browser sets — model_io.py /
policy.py fill the same kwargs, so the wording differs but the wiring is the same.

⚠️ DRAFT — un-tuned. Validated structurally (placeholders + construction); the prompt
QUALITY needs real Android-task A/B tuning. iphone remains the package default;
android injects this set via adapters/android/factory.py.
"""

from gui_agent.prompts import load_prompt_text

DECOMPOSE_PROMPT = load_prompt_text("task.milestone.android.decompose")

SINGLE_CHECKER_PROMPT = load_prompt_text("task.milestone.android.checker")

# ── Per-kind checker sections (only the relevant one is injected) ──────────
_CHECK_SECTION_NAVIGATION = load_prompt_text("context.milestone.android.check.navigation")

_CHECK_SECTION_FILTER = load_prompt_text("context.milestone.android.check.filter")

_CHECK_SECTION_ACTION = load_prompt_text("context.milestone.android.check.action")

_CHECK_SECTION_COLLECTION = load_prompt_text("context.milestone.android.check.collection")

_CHECK_SECTION_CONVERGE = load_prompt_text("context.milestone.android.check.converge")

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

LOOP_FRAME_PROMPT = load_prompt_text("task.milestone.android.loop_frame")

PLAN_PROMPT = load_prompt_text("task.milestone.android.planner")

LOOP_SCROLL_PROMPT = load_prompt_text("task.milestone.android.loop_scroll")

REPLAN_PROMPT = load_prompt_text("task.milestone.android.replanner")

STOP_CONDITION_PATCH_PROMPT = load_prompt_text("task.milestone.android.stop_condition_patch")


# ── Bundle into the neutral MilestonePrompts seam (android draft) ────────────
from gui_agent.core.supervisor.milestone.schemas import MilestonePrompts  # noqa: E402

ANDROID_MILESTONE_PROMPTS = MilestonePrompts(
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
    home_identity_markers=("Android 主屏幕", "主屏幕", "主屏", "home screen", "launcher"),
)
