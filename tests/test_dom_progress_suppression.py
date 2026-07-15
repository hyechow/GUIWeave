"""DOM interactive-state progress signal suppresses false instruction-repetition stuck.

Regression for 20260612_103356: an 8-field form fill produces legitimately similar
instructions ("在X输入框输入Y") on a near-identical screen — text similarity escalated
real progress to stuck. The browser observes a form-state fingerprint
(Observation.dom_state); a changed fingerprint is ground truth that the last action
worked, and lets the "repeated" plan through instead of retry→escalate.
"""

from __future__ import annotations

from gui_agent.adapters.browser.actions import BrowserActionDecision
from gui_agent.core.schemas import Milestone, Observation, SupervisorStep
from gui_agent.core.supervisor.milestone.policy import MilestoneSupervisorPolicy
from gui_agent.core.supervisor.milestone.schemas import _PlanResult, _SingleCheckResult

STUCK_SUMMARY = "__STUCK_SENTINEL__"


def _policy(dom_changed: bool) -> MilestoneSupervisorPolicy:
    p = MilestoneSupervisorPolicy()
    p._monitor.dom_changed = dom_changed
    p._invoke_planner = lambda *a, **k: _PlanResult(  # type: ignore[method-assign]
        instruction="在「初始电量」输入框中输入90", summary="填表"
    )
    p._is_repeated_instruction = lambda *a, **k: True  # type: ignore[method-assign]
    p._handle_stuck = lambda *a, **k: SupervisorStep(  # type: ignore[method-assign]
        should_act=False, summary=STUCK_SUMMARY
    )
    return p


def _ms() -> Milestone:
    return Milestone.model_validate(
        {"id": "m1", "name": "填写配置", "description": "d", "success_condition": "s", "kind": "action"}
    )


def _check() -> _SingleCheckResult:
    return _SingleCheckResult(status="in_progress", effect_status="unverified", reason="尚未完成填写表单", summary="填表中")


def _obs() -> Observation:
    return Observation(png_bytes=b"png", source="test")


def test_dom_changed_lets_repeated_looking_plan_through():
    # 指纹在变 = 上一步确实生效(填表推进) → 相似指令直接放行,不重试不升级
    step = _policy(dom_changed=True)._plan_single(_ms(), _check(), _obs(), [])
    assert step.summary != STUCK_SUMMARY
    assert step.should_act and "输入90" in (step.instruction or "")


def test_no_dom_change_does_not_make_planner_text_a_dispatch_gate():
    step = _policy(dom_changed=False)._plan_single(_ms(), _check(), _obs(), [])
    assert step.summary != STUCK_SUMMARY
    assert step.should_act


def test_dom_state_presence_disables_pre_action_text_repeat_guard():
    # 浏览器有 DOM 指纹时，planner 文本只代表意图；真实重复要等 action_policy/executor
    # 产生 DOM action signature 后判断。即使上一帧没有 dom_changed，也不能在这里靠文本挡住。
    obs = Observation(png_bytes=b"png", source="browser", url="http://h/admin/catalog/product", dom_state="abc")
    step = _policy(dom_changed=False)._plan_single(_ms(), _check(), obs, [])
    assert step.summary != STUCK_SUMMARY
    assert step.should_act


def test_observation_dom_state_optional():
    # dom_state 是平台可选信号:不提供时为 None(iphone/android),提供时原样保存
    assert _obs().dom_state is None
    assert Observation(png_bytes=b"p", source="t", dom_state="abc123").dom_state == "abc123"


def _browser_tap_decision() -> BrowserActionDecision:
    return BrowserActionDecision.model_validate({
        "action": {
            "action_type": "tap",
            "x": 120,
            "y": 250,
            "description": "点击 Edit",
            "snap": {"snapped": [120, 250], "info": "button 72x32"},
        }
    })


def _step() -> SupervisorStep:
    return SupervisorStep(
        should_act=True,
        instruction="点击 Edit",
        summary="打开详情",
        milestone_id="m1",
        completion_strategy="visible_once",
    )


def test_executed_dom_action_signature_is_recorded():
    p = MilestoneSupervisorPolicy()
    obs = Observation(png_bytes=b"png", source="browser", url="http://h/admin/catalog/product", dom_state="row=sku-a")

    p.note_executed_action(
        index=1,
        observation=obs,
        supervisor_step=_step(),
        action_decision=_browser_tap_decision(),
        executed=True,
    )

    trace = p._monitor.render()
    assert "状态:/admin/catalog/product" in trace
    assert "决策:tap|button@" in trace
    assert "交互:row=sku-" in trace


def test_same_dom_action_on_different_dom_state_is_not_repeat():
    p = MilestoneSupervisorPolicy()
    for index, dom_state in [(1, "row=sku-a"), (2, "row=sku-b")]:
        p.note_executed_action(
            index=index,
            observation=Observation(
                png_bytes=b"png",
                source="browser",
                url="http://h/admin/catalog/product",
                dom_state=dom_state,
            ),
            supervisor_step=_step(),
            action_decision=_browser_tap_decision(),
            executed=True,
        )

    assert "⚠️重复" not in p._monitor.render()


def test_same_dom_action_on_same_dom_state_marks_repeat():
    p = MilestoneSupervisorPolicy()
    obs = Observation(png_bytes=b"png", source="browser", url="http://h/admin/catalog/product", dom_state="row=sku-a")
    for index in [1, 2]:
        p.note_executed_action(
            index=index,
            observation=obs,
            supervisor_step=_step(),
            action_decision=_browser_tap_decision(),
            executed=True,
        )

    trace = p._monitor.render()
    assert "⚠️重复(同 T1)" in trace
    assert p.constraints_snapshot()
