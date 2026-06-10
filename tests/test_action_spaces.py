"""Per-platform action-space isolation + serialization guards.

Each platform's Action subclasses the neutral BaseAction with its OWN action_type
vocabulary (and fields); a policy injects only its platform's schema into the LLM, so
no cross-platform action leaks. These tests pin that isolation + the SerializeAsAny
guard that keeps subclass fields through model_dump_json (context.json).
"""

from __future__ import annotations

from typing import get_args

import pytest

from gui_agent.core.schemas import BaseAction, BaseActionDecision
from gui_agent.adapters.iphone.actions import IPhoneAction, IPhoneActionDecision
from gui_agent.adapters.browser.actions import BrowserAction, BrowserActionDecision
from gui_agent.adapters.android.actions import AndroidAction, AndroidActionDecision


def _action_type_values(action_cls) -> set[str]:
    return set(get_args(action_cls.model_fields["action_type"].annotation))


# --- action_type vocabulary: overlap + per-platform isolation --------------- #
def test_shared_overlap_in_every_platform():
    shared = {"tap", "type", "clear_text", "press_enter", "scroll", "drag", "stop"}
    for cls in (IPhoneAction, BrowserAction, AndroidAction):
        assert shared <= _action_type_values(cls)


def test_iphone_vocabulary():
    v = _action_type_values(IPhoneAction)
    assert {"home", "app_switch"} <= v        # app_switch shared with android
    assert "navigate" not in v
    assert "back" not in v


def test_browser_vocabulary():
    v = _action_type_values(BrowserAction)
    assert "navigate" in v
    assert "home" not in v
    assert "app_switch" not in v and "back" not in v


def test_android_vocabulary():
    v = _action_type_values(AndroidAction)
    assert {"home", "back", "app_switch"} <= v
    assert "navigate" not in v


# --- construction: positive + negative (rejects foreign action_type) -------- #
def test_positive_construction():
    IPhoneAction(action_type="app_switch", description="打开切换器")
    IPhoneAction(action_type="drag", target_area="picker_left", value_direction="increase", description="调大年份")
    BrowserAction(action_type="navigate", url="example.com", description="打开网站")
    AndroidAction(action_type="back", description="返回")
    AndroidAction(action_type="app_switch", description="切换应用")


@pytest.mark.parametrize(
    "cls,bad",
    [
        (IPhoneAction, "navigate"),
        (IPhoneAction, "back"),
        (BrowserAction, "home"),
        (BrowserAction, "app_switch"),
        (BrowserAction, "back"),
        (AndroidAction, "navigate"),
    ],
)
def test_rejects_foreign_action_type(cls, bad):
    with pytest.raises(Exception):
        cls(action_type=bad, description="x")


def test_browser_navigate_requires_url():
    with pytest.raises(Exception):
        BrowserAction(action_type="navigate", description="x")  # no url


# --- isinstance: subclasses satisfy the neutral base ------------------------ #
def test_subclasses_are_base():
    a = IPhoneAction(action_type="tap", x=1, y=1, description="x")
    assert isinstance(a, BaseAction)
    d = IPhoneActionDecision(action=a)
    assert isinstance(d, BaseActionDecision)
    assert isinstance(BrowserActionDecision(action=BrowserAction(action_type="stop", description="x")), BaseActionDecision)
    assert isinstance(AndroidActionDecision(action=AndroidAction(action_type="home", description="x")), BaseActionDecision)


# --- SerializeAsAny: subclass fields survive model_dump_json (context.json) -- #
def test_serialization_preserves_subclass_fields():
    from gui_agent.core.schemas import PolicyContext, PolicyTurn, SupervisorStep

    ip = IPhoneActionDecision(action=IPhoneAction(
        action_type="drag", target_area="picker_left", value_direction="increase", description="调大"))
    br = BrowserActionDecision(action=BrowserAction(
        action_type="navigate", url="example.com", description="打开"))
    sv = SupervisorStep(should_act=True, stop=False, goal_completed=False, summary="x")
    ctx = PolicyContext(
        goal="g", supervisor_policy_name="milestone", action_policy_name="x",
        turns=[
            PolicyTurn(index=0, observation_source="iphone", supervisor=sv, action_decision=ip),
            PolicyTurn(index=1, observation_source="browser", supervisor=sv, action_decision=br),
        ],
    )
    dumped = ctx.model_dump_json()
    assert "value_direction" in dumped and "increase" in dumped  # iphone field survived
    assert "example.com" in dumped                                # browser url survived


# --- field isolation: a platform's injected schema has ONLY its own fields --- #
def _schema_str(decision_cls) -> str:
    import json

    return json.dumps(decision_cls.model_json_schema(), ensure_ascii=False)


def test_iphone_schema_has_picker_not_url():
    s = _schema_str(IPhoneActionDecision)
    assert "value_direction" in s and "picker_left" in s  # iphone picker fields present
    assert "url" not in s  # browser field NOT leaked


def test_browser_schema_has_url_not_picker():
    s = _schema_str(BrowserActionDecision)
    assert "url" in s
    assert "value_direction" not in s and "picker_left" not in s  # iphone fields NOT leaked


def test_android_schema_has_neither_picker_nor_url():
    s = _schema_str(AndroidActionDecision)
    assert "url" not in s and "value_direction" not in s and "picker_left" not in s


def test_base_rejects_all_platform_actions():
    for bad in ("navigate", "home", "back", "app_switch"):
        with pytest.raises(Exception):
            BaseAction(action_type=bad, description="x")
