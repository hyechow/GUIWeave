from __future__ import annotations

import gui_agent.core.supervisor.milestone.policy as policy_mod
from gui_agent.core.schemas import Milestone, Observation
from gui_agent.core.supervisor.milestone.helpers import target_affordance_scroll_plan


class _CheckerReached(Exception):
    pass


def _notify_milestone() -> Milestone:
    return Milestone(
        id="m_notify",
        name=(
            "在 'Notes for this Order' 表单的 Comment 栏填入 'sorry we are bankrupt'，"
            "勾选 'Notify Customer by Email'，点击 Update"
        ),
        description=(
            "在 'Notes for this Order' 表单的 Comment 栏填入 'sorry we are bankrupt'，"
            "勾选 'Notify Customer by Email'，点击 Update"
        ),
        success_condition="页面显示成功提示或评论历史中新增了对应备注",
        kind="action",
    )


def _offscreen_controls() -> list[dict]:
    return [
        {
            "kind": "native_select",
            "label": "Status",
            "rect": {"x": 349, "y": 1558, "w": 200, "h": 32},
            "in_viewport": False,
            "viewport_pos": "below",
        },
        {
            "kind": "textarea",
            "label": "Comment",
            "rect": {"x": 467, "y": 1646, "w": 400, "h": 120},
            "in_viewport": False,
            "viewport_pos": "below",
        },
        {
            "kind": "checkbox_input",
            "label": "Notify Customer by Email",
            "value": "off",
            "rect": {"x": 321, "y": 1714, "w": 24, "h": 24},
            "in_viewport": False,
            "viewport_pos": "below",
        },
    ]


def test_target_affordance_scroll_plan_uses_offscreen_dom_controls() -> None:
    plan = target_affordance_scroll_plan(_offscreen_controls(), _notify_milestone())

    assert plan is not None
    assert plan.direction == "down"
    assert "Comment" in plan.instruction
    assert "滚动" in plan.instruction
    assert "DOM" in plan.summary


def test_policy_acquire_gate_bypasses_checker_for_known_offscreen_controls(monkeypatch) -> None:
    checker_calls: list[int] = []

    def _spy_run_checker(*_args, **_kwargs):
        checker_calls.append(1)
        raise _CheckerReached()

    monkeypatch.setattr(policy_mod, "run_checker", _spy_run_checker)
    monkeypatch.setattr(policy_mod, "is_loading_frame", lambda _obs: False)

    policy = policy_mod.MilestoneSupervisorPolicy()
    policy.reseed(_notify_milestone())
    obs = Observation(
        png_bytes=b"\x89PNG\r\n\x1a\n",
        source="browser",
        url="http://example.test/admin/sales/order/view/order_id/65/",
        form_controls=_offscreen_controls(),
        dom_state="order-detail-controls-below",
    )

    step = policy.step(obs, goal="notify customer", history=[])

    assert checker_calls == []
    assert step.should_act is True
    assert step.direction == "down"
    assert step.instruction and "Comment" in step.instruction
    assert "Edit" not in step.instruction


def test_target_affordance_gate_continues_until_all_targets_are_visible() -> None:
    controls = _offscreen_controls()
    controls[1] = {
        **controls[1],
        "in_viewport": True,
        "viewport_pos": "in",
        "rect": {"x": 467, "y": 646, "w": 400, "h": 120},
    }

    plan = target_affordance_scroll_plan(controls, _notify_milestone())

    assert plan is not None
    assert plan.direction == "down"
    assert "Notify Customer by Email" in plan.instruction


def test_target_affordance_gate_stays_out_when_all_target_controls_are_visible() -> None:
    controls = _offscreen_controls()
    controls[1] = {
        **controls[1],
        "in_viewport": True,
        "viewport_pos": "in",
        "rect": {"x": 467, "y": 646, "w": 400, "h": 120},
    }
    controls[2] = {
        **controls[2],
        "in_viewport": True,
        "viewport_pos": "in",
        "rect": {"x": 321, "y": 714, "w": 24, "h": 24},
    }

    assert target_affordance_scroll_plan(controls, _notify_milestone()) is None


def _return_location_milestone() -> Milestone:
    # After a terminal Submit succeeded and redirected, the return-contract recovery re-opens the
    # milestone to locate the return field, leaking the field name into the milestone text.
    return Milestone(
        id="m_submit",
        name="点 Submit Shipment（继续定位返回字段：submit_status）",
        description="提交发货单后读取 submit_status",
        success_condition="发货已保存",
        kind="action",
    )


def _order_status_control() -> list[dict]:
    # The order/comment "Status" dropdown, offscreen. Its label "Status" is a SUBSTRING of the
    # milestone's return-field token "submit_status" — must NOT be treated as a named target.
    return [
        {
            "kind": "native_select",
            "label": "Status",
            "rect": {"x": 349, "y": 1558, "w": 200, "h": 32},
            "in_viewport": False,
            "viewport_pos": "below",
        },
    ]


def test_acquire_gate_ignores_status_substring_of_submit_status() -> None:
    # Regression for WebArena 499 (20260708_165316): AcquireGate matched "Status" against the
    # return-field token "submit_status" and scrolled to the order Status dropdown, amplifying a
    # return-contract violation into a stuck loop. Word-boundary matching must reject it.
    plan = target_affordance_scroll_plan(_order_status_control(), _return_location_milestone())
    assert plan is None


def test_acquire_gate_still_matches_standalone_status_control() -> None:
    # But a milestone that genuinely targets a standalone "Status" control still acquires it.
    ms = Milestone(
        id="m_status",
        name="将 Status 下拉设为 Processing",
        description="",
        success_condition="Status 已设为 Processing",
        kind="action",
    )
    plan = target_affordance_scroll_plan(_order_status_control(), ms)
    assert plan is not None
    assert plan.direction == "down"
