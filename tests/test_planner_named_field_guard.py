from gui_agent.core.schemas import Milestone, Observation
from gui_agent.core.supervisor.milestone.helpers import (
    _guard_named_field_substitution_plan,
    _guard_stale_text_filter_plan,
)
from gui_agent.core.supervisor.milestone.schemas import _PlanResult, _SingleCheckResult


def _check() -> _SingleCheckResult:
    return _SingleCheckResult(status="in_progress", reason="尚未筛选", summary="等待操作")


def test_named_field_guard_does_not_substitute_visible_other_field():
    milestone = Milestone.model_validate({
        "id": "m",
        "name": "在产品列使用关键词'Olivia'进行模糊搜索筛选",
        "description": "在产品列使用关键词'Olivia'进行模糊搜索筛选",
        "success_condition": "产品列已按 Olivia 筛选",
        "kind": "action",
    })
    observation = Observation(
        png_bytes=b"png",
        source="browser",
        form_controls=[
            {"label": "nickname", "kind": "text_input"},
            {"label": "detail", "kind": "text_input"},
            {"label": "sku", "kind": "text_input"},
        ],
    )
    plan = _PlanResult(instruction="在 'Nickname' 输入框输入 Olivia", summary="误把目标字段换成可见字段")

    guarded = _guard_named_field_substitution_plan(plan, milestone, _check(), observation)

    assert "产品" in guarded.instruction
    assert "Nickname" not in guarded.instruction
    assert guarded.direction == "right"


def test_named_field_guard_retargets_when_target_control_is_visible():
    milestone = Milestone.model_validate({
        "id": "m",
        "name": "在 Product 列使用关键词 Olivia 进行筛选",
        "description": "在 Product 列使用关键词 Olivia 进行筛选",
        "success_condition": "Product 列已按 Olivia 筛选",
        "kind": "action",
    })
    observation = Observation(
        png_bytes=b"png",
        source="browser",
        form_controls=[
            {"label": "Nickname", "kind": "text_input"},
            {"label": "Product", "kind": "text_input"},
        ],
    )
    plan = _PlanResult(instruction="在 Nickname 输入框输入 Olivia", summary="误指向 Nickname")

    guarded = _guard_named_field_substitution_plan(plan, milestone, _check(), observation)

    assert guarded.instruction == "在 Product 输入框输入 Olivia"
    assert guarded.direction is None


def test_stale_filter_guard_blocks_submitting_old_dom_value():
    milestone = Milestone.model_validate({
        "id": "m",
        "name": "清除当前筛选，在 Product 列改用关键词 'Olivia' 重新提交筛选",
        "description": "当前精确词 'Olivia zip jacket' 已提交但返回 0 records found；本步必须把 Product 列筛选词改成关键词 'Olivia'。",
        "success_condition": "Product 列筛选框 current='Olivia'，且提交后列表显示非 0 条结果",
        "kind": "action",
    })
    observation = Observation(
        png_bytes=b"png",
        source="browser",
        form_controls=[
            {"label": "Product", "kind": "text_input", "value": "Olivia zip jacket"},
        ],
    )
    plan = _PlanResult(instruction="点击 'Search' 按钮提交筛选操作", summary="提交旧筛选")

    guarded = _guard_stale_text_filter_plan(plan, milestone, _check(), observation)

    assert guarded.instruction == "在 Product 输入框输入 Olivia"
    assert "不能提交旧值" in guarded.summary


def test_stale_filter_guard_allows_explicit_reset_before_retyping():
    milestone = Milestone.model_validate({
        "id": "m",
        "name": "清除当前筛选，在 Product 列改用关键词 'Olivia' 重新提交筛选",
        "description": "本步必须把 Product 列筛选词改成关键词 'Olivia'。",
        "success_condition": "Product 列筛选框 current='Olivia'",
        "kind": "action",
    })
    observation = Observation(
        png_bytes=b"png",
        source="browser",
        form_controls=[
            {"label": "Product", "kind": "text_input", "value": "Olivia zip jacket"},
        ],
    )
    plan = _PlanResult(instruction="点击 'Reset Filter' 按钮清除当前筛选", summary="先清除")

    guarded = _guard_stale_text_filter_plan(plan, milestone, _check(), observation)

    assert guarded.instruction == plan.instruction
