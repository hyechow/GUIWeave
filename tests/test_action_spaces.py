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
    shared = {"tap", "type", "clear_text", "press_enter", "scroll", "drag"}
    for cls in (IPhoneAction, BrowserAction, AndroidAction):
        assert shared <= _action_type_values(cls)
        assert "stop" not in _action_type_values(cls)


def test_iphone_vocabulary():
    v = _action_type_values(IPhoneAction)
    assert {"home", "app_switch"} <= v        # app_switch shared with android
    assert "navigate" not in v
    assert "back" not in v


def test_browser_vocabulary():
    v = _action_type_values(BrowserAction)
    assert "navigate" in v and "back" in v   # back = history back (shared with android)
    assert {"new_tab", "select_tab", "close_tab"} <= v   # tab management
    assert "upload" in v                     # file upload via file chooser
    assert "select_option" in v              # native/browser dropdown option selection
    assert "home" not in v
    assert "app_switch" not in v


def test_android_vocabulary():
    v = _action_type_values(AndroidAction)
    assert {"home", "back", "app_switch"} <= v
    assert "navigate" not in v


# --- construction: positive + negative (rejects foreign action_type) -------- #
def test_positive_construction():
    IPhoneAction(action_type="app_switch", description="打开切换器")
    IPhoneAction(action_type="drag", target_area="picker_left", value_direction="increase", description="调大年份")
    BrowserAction(action_type="navigate", url="example.com", description="打开网站")
    BrowserAction(action_type="new_tab", url="feishu.cn", description="新标签页打开飞书")
    BrowserAction(action_type="select_tab", tab_match="飞书", description="切到飞书标签页")
    BrowserAction(action_type="close_tab", description="关当前标签页")
    BrowserAction(action_type="upload", x=500, y=500, file_path="~/Downloads/m.map_export", description="上传地图文件")
    BrowserAction(action_type="select_option", x=500, y=500, text="Complete", description="选择状态")
    AndroidAction(action_type="back", description="返回")
    AndroidAction(action_type="app_switch", description="切换应用")


def test_select_tab_requires_match():
    with pytest.raises(Exception):
        BrowserAction(action_type="select_tab", description="切标签")  # no tab_match


def test_browser_upload_requires_file_path():
    with pytest.raises(Exception):
        BrowserAction(action_type="upload", x=1, y=1, description="x")  # no file_path


def test_browser_select_option_requires_text():
    with pytest.raises(Exception):
        BrowserAction(action_type="select_option", x=1, y=1, description="x")  # no option text


@pytest.mark.parametrize(
    "cls,bad",
    [
        (IPhoneAction, "navigate"),
        (IPhoneAction, "back"),
        (BrowserAction, "home"),
        (BrowserAction, "app_switch"),
        (AndroidAction, "navigate"),
        (IPhoneAction, "upload"),
        (AndroidAction, "upload"),
        (IPhoneAction, "select_option"),
        (AndroidAction, "select_option"),
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
    assert isinstance(
        BrowserActionDecision(action=None, not_found_reason="目标不可定位"),
        BaseActionDecision,
    )
    assert isinstance(AndroidActionDecision(action=AndroidAction(action_type="home", description="x")), BaseActionDecision)


# --- SerializeAsAny: subclass fields survive model_dump_json (context.json) -- #
def test_serialization_preserves_subclass_fields():
    from gui_agent.core.schemas import PolicyContext, PolicyTurn, SupervisorStep

    ip = IPhoneActionDecision(action=IPhoneAction(
        action_type="drag", target_area="picker_left", value_direction="increase", description="调大"))
    br = BrowserActionDecision(action=BrowserAction(
        action_type="navigate", url="example.com", description="打开"))
    sv = SupervisorStep(should_act=True, summary="x")
    ctx = PolicyContext(
        goal="g", supervisor_policy_name="statement", action_policy_name="x",
        journal={"events": [
            PolicyTurn(index=0, observation_source="iphone", supervisor=sv, action_decision=ip),
            PolicyTurn(index=1, observation_source="browser", supervisor=sv, action_decision=br),
        ]},
    )
    dumped = ctx.model_dump_json()
    assert "value_direction" in dumped and "increase" in dumped  # iphone field survived
    assert "example.com" in dumped                                # browser url survived


# --- _unwrap_flat_action repairs malformed action wrappers (model quirks) ------ #
def test_action_decision_unwraps_bare_string_action():
    # json_object mode sometimes emits the action TYPE as a bare string instead of the nested
    # object (e.g. {"action":"tap","x":..}); repair it so the primary parse succeeds.
    d = BrowserActionDecision.model_validate(
        {"action": "tap", "x": 67, "y": 175, "description": "点订单菜单"})
    assert d.action.action_type == "tap" and d.action.x == 67 and d.action.y == 175
    d2 = BrowserActionDecision.model_validate(
        {"action": "navigate", "url": "http://x:22000", "description": "打开", "not_found_reason": None})
    assert d2.action.action_type == "navigate" and d2.action.url == "http://x:22000"


def test_action_decision_unwraps_flat_fields():
    # action fields at the top level, no wrapper at all
    d = IPhoneActionDecision.model_validate({"action_type": "tap", "x": 1, "y": 2, "description": "x"})
    assert d.action.action_type == "tap" and d.action.x == 1


def test_legacy_stop_decision_is_migrated_to_grounding_failure():
    decision = BrowserActionDecision.model_validate({
        "action": {"action_type": "stop", "description": "当前帧找不到目标"},
    })

    assert decision.action is None
    assert decision.not_found_reason == "当前帧找不到目标"


def test_no_action_requires_grounding_reason():
    with pytest.raises(Exception):
        BrowserActionDecision(action=None)


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


def test_base_round_trips_platform_action_types():
    # The base is the RELOAD shape: a persisted PolicyTurn is validated through BaseAction,
    # so it must accept any platform action_type on load (the subclass already enforced its
    # own vocabulary when the action was first created). A closed Literal here broke
    # checkpoint resume and replay for any browser run with select_option / navigate / ….
    # Cross-platform isolation is still enforced by test_rejects_foreign_action_type.
    for persisted in ("navigate", "home", "back", "app_switch", "select_option"):
        BaseAction(action_type=persisted, description="x")


def test_type_empty_text_coerces_to_clear_text():
    """A `type` action with empty text (the model's common way of saying「清空输入框」) must coerce
    to the proper `clear_text` action, NOT raise — one malformed action decision should never crash
    the whole run (live: type x=247 text="" 「清空 Search by keyword」 → ValidationError → run died)."""
    for cls in (BaseAction, BrowserAction):
        a = cls.model_validate(
            {"action_type": "type", "x": 247, "y": 375, "text": "", "description": "清空输入框"}
        )
        assert a.action_type == "clear_text"
    # a normal type with text is untouched
    b = BrowserAction.model_validate(
        {"action_type": "type", "x": 1, "y": 2, "text": "hello", "description": "输入"}
    )
    assert b.action_type == "type" and b.text == "hello"
