"""DOM interactive-state progress signal suppresses false instruction-repetition stuck.

Regression for 20260612_103356: an 8-field form fill produces legitimately similar
instructions ("在X输入框输入Y") on a near-identical screen — text similarity escalated
real progress to stuck. The browser observes a form-state fingerprint
(Observation.dom_state); a changed fingerprint is ground truth that the last action
worked, and lets the "repeated" plan through instead of retry→escalate.
"""

from __future__ import annotations

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
        should_act=False, stop=False, goal_completed=False, summary=STUCK_SUMMARY
    )
    return p


def _ms() -> Milestone:
    return Milestone.model_validate(
        {"id": "m1", "name": "填写配置", "description": "d", "success_condition": "s", "kind": "action"}
    )


def _check() -> _SingleCheckResult:
    return _SingleCheckResult(status="in_progress", reason="尚未完成填写表单", summary="填表中")


def _obs() -> Observation:
    return Observation(png_bytes=b"png", source="test")


def test_dom_changed_lets_repeated_looking_plan_through():
    # 指纹在变 = 上一步确实生效(填表推进) → 相似指令直接放行,不重试不升级
    step = _policy(dom_changed=True)._plan_single(_ms(), _check(), _obs(), [])
    assert step.summary != STUCK_SUMMARY
    assert step.should_act and "输入90" in (step.instruction or "")


def test_no_dom_change_still_escalates_to_stuck():
    # 指纹没变 → 维持原行为:重试一次仍重复 → 升级 stuck
    step = _policy(dom_changed=False)._plan_single(_ms(), _check(), _obs(), [])
    assert step.summary == STUCK_SUMMARY


def test_observation_dom_state_optional():
    # dom_state 是平台可选信号:不提供时为 None(iphone/android),提供时原样保存
    assert _obs().dom_state is None
    assert Observation(png_bytes=b"p", source="t", dom_state="abc123").dom_state == "abc123"
