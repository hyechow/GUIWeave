from __future__ import annotations

from gui_agent.core.schemas import Milestone, Observation
from gui_agent.core.supervisor.milestone.helpers import (
    _advance_native_multiselect_plan,
    _format_form_controls,
    _guard_native_select_plan,
    native_select_satisfies_target,
)


def _cg_control(selected: str):
    return {
        "kind": "native_select", "name": "customer_group_ids", "label": "Customer Groups",
        "selected_text": selected,
        "options": ["NOT LOGGED IN", "General", "Wholesale", "Retailer"],
    }


def _ms(sc: str, name: str = "设置 Customer Groups"):
    return Milestone.model_validate({"id": "m", "name": name, "description": name,
                                     "success_condition": sc, "kind": "action"})


def test_native_select_gate_fires_when_dom_selection_satisfies_target():
    # obs.dom selected_text is authoritative for control.selected — a select-focused milestone whose
    # target value is already selected is deterministically done, even if the vision checker would
    # loop "list box still open = not selected" (WebArena 702).
    fc = [_cg_control("General")]
    assert native_select_satisfies_target(fc, _ms("Customer Groups 已选中 General"))
    # multi-value target, all present
    assert native_select_satisfies_target([_cg_control("General, Wholesale, Retailer")],
                                          _ms("Customer Groups 选中 General、Wholesale、Retailer"))


def _plan(instr):
    return _PlanResult(instruction=instr, summary="")


def _obs(selected):
    return Observation(png_bytes=b"png", source="browser", form_controls=[_cg_control(selected)])


def test_advance_multiselect_skips_already_selected_to_next_target():
    # WebArena 702 loop: planner keeps "select General" though DOM shows it selected. Advance to the
    # next milestone-target option not yet selected instead of re-selecting.
    ms = _ms("Customer Groups 选中 General、Wholesale、Retailer")
    out = _advance_native_multiselect_plan(_plan("在 Customer Groups 下拉框选择 General"), ms, _obs("General"))
    assert "Wholesale" in out.instruction and "选择" in out.instruction
    # once General+Wholesale are in, advance to Retailer
    out2 = _advance_native_multiselect_plan(_plan("在 Customer Groups 下拉框选择 General"), ms, _obs("General, Wholesale"))
    assert "Retailer" in out2.instruction


def test_advance_multiselect_noop_cases():
    ms = _ms("Customer Groups 选中 General、Wholesale、Retailer")
    # all targets already selected → nothing to advance to, plan unchanged
    same = _advance_native_multiselect_plan(_plan("在 Customer Groups 下拉框选择 General"), ms, _obs("General, Wholesale, Retailer"))
    assert same.instruction == "在 Customer Groups 下拉框选择 General"
    # the option being selected is NOT yet selected → not a redundant re-select, leave it
    keep = _advance_native_multiselect_plan(_plan("在 Customer Groups 下拉框选择 General"), ms, _obs(""))
    assert keep.instruction == "在 Customer Groups 下拉框选择 General"


def test_native_select_gate_conservative_misses():
    # not selected yet → not done
    assert not native_select_satisfies_target([_cg_control("")], _ms("Customer Groups 已选中 General"))
    # target only partially selected → not done
    assert not native_select_satisfies_target([_cg_control("General")],
                                              _ms("Customer Groups 选中 General、Wholesale、Retailer"))
    # compound "…and save" milestone is NOT fast-pathed (other fields/save remain)
    assert not native_select_satisfies_target(
        [_cg_control("General")],
        _ms("填写 Customer Groups 选 General 后保存规则", name="创建规则"))
    # target can't be pinned down (milestone doesn't name the select) → conservative False
    assert not native_select_satisfies_target([_cg_control("General")], _ms("完成表单"))
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
