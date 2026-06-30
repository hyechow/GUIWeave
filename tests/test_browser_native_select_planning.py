from __future__ import annotations

from gui_agent.core.schemas import Milestone, Observation
from gui_agent.core.supervisor.milestone.helpers import (
    _format_form_controls,
    _guard_native_select_plan,
)
from gui_agent.core.supervisor.milestone.schemas import _PlanResult, _SingleCheckResult


def test_format_form_controls_is_dom_fact_only_no_planner_directive():
    # The form-controls context block is a DOM FACT surface: control type, current value,
    # candidate options. Planner HOW ("plan as select/set, don't click-expand") was relocated to
    # planner.md, and the deterministic _guard_native_select_plan (below) enforces the behavior —
    # so the fact block must NOT carry that directive (keeps observation vs directive separated).
    text = _format_form_controls([{
        "kind": "native_select",
        "label": "Status",
        "options": ["Canceled", "Complete", "Processing"],
        "rect": {"x": 856, "y": 509},
    }])

    assert "浏览器 DOM 表单控件" in text
    assert "Status: native_select" in text
    assert "Complete" in text
    # fact-only boundary: no planner instruction text leaked into the observation block
    assert "不要规划为" not in text
    assert "应规划为" not in text


def test_native_select_guard_rewrites_open_click_plan_to_select_value():
    plan = _PlanResult(
        instruction="点击 Status 下拉框以展开选项列表",
        summary="需要打开状态选项",
    )
    milestone = Milestone.model_validate({
        "id": "m1",
        "name": "设置筛选条件 Status = Complete",
        "description": "将订单 Status 筛选为 Complete",
        "success_condition": "Status = Complete 筛选已应用",
        "kind": "filter",
    })
    check = _SingleCheckResult(
        status="in_progress",
        reason="Status 尚未设置为 Complete",
        summary="筛选区已展开",
    )
    obs = Observation(
        png_bytes=b"png",
        source="browser",
        form_controls=[{
            "label": "Status",
            "kind": "native_select",
            "options": ["Canceled", "Complete", "Processing"],
        }],
    )

    guarded = _guard_native_select_plan(plan, milestone, check, obs)

    assert guarded.instruction == "在 Status 下拉框选择 Complete"
    assert "native select" in guarded.summary
