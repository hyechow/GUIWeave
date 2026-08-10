"""BrowserExecutor._tap wires device.dom_snap → clicks the snapped point and records action.snap.

The snap METRIC (device.dom_snap, elementFromPoint + conservative guards) needs a live browser,
so here we lock the executor wiring with a fake client: a snapped result moves the click, a
no-snap (info=None) keeps it, a snap failure falls back to the original point (never blocks a
click), and going through execute() records action.snap (normalized) so the report / runtime
visualizer draw original→snapped — the same as iphone YOLO/OCR.
"""

from __future__ import annotations

import types

from gui_agent.adapters.browser.actions import BrowserAction, BrowserActionDecision
from gui_agent.adapters.browser.executor import BrowserExecutor


class _FakeClient:
    def __init__(self, snap, viewport=(1000, 1000)):
        self._snap = snap  # (sx, sy, info) tuple OR an Exception to raise
        self._viewport = viewport
        self.clicked = None
        self.seen_target = None  # target_text the executor passed to dom_snap

    @property
    def viewport_size(self):
        return self._viewport

    def dom_snap(self, x, y, target_text=""):
        self.seen_target = target_text
        if isinstance(self._snap, Exception):
            raise self._snap
        return self._snap

    def tap(self, x, y):
        self.clicked = (x, y)
        return f"OK tap ({x:.0f},{y:.0f})"


def _exec(client) -> BrowserExecutor:
    return BrowserExecutor(types.SimpleNamespace(client=client))


def test_tap_snaps_to_dom_center():
    c = _FakeClient((635.0, 620.0, "div 666x28"))   # near-miss → snapped to row center
    assert _exec(c)._tap(342, 616) is True
    assert c.clicked == (635.0, 620.0)


def test_tap_no_snap_keeps_point():
    c = _FakeClient((342.0, 616.0, None))            # canvas / huge / no clickable → no snap
    _exec(c)._tap(342, 616)
    assert c.clicked == (342, 616)


def test_tap_snap_failure_falls_back_to_original():
    c = _FakeClient(RuntimeError("cdp down"))         # snap errored → never block the click
    _exec(c)._tap(342, 616)
    assert c.clicked == (342, 616)


def test_vision_only_tool_agent_can_disable_dom_snap():
    c = _FakeClient((635.0, 620.0, "div 666x28"))
    executor = _exec(c)
    executor.disable_dom_snap = True

    assert executor._tap(342, 616) is True
    assert c.seen_target is None
    assert c.clicked == (342, 616)


def test_enhanced_coordinate_grounding_failure_keeps_visual_decision():
    executor = BrowserExecutor(types.SimpleNamespace(client=None))
    decision = BrowserActionDecision(action=BrowserAction(
        action_type="tap",
        x=342,
        y=616,
        description="Tap the visible control in the center content area",
    ))

    grounded = executor.ground_coordinates(
        decision,
        [{"kind": "button", "rect": {"x": 350, "y": 620, "w": 80, "h": 30}}],
    )

    assert grounded is decision
    assert (grounded.action.x, grounded.action.y) == (342, 616)


def test_unresolved_typed_target_does_not_create_an_executor_protocol():
    c = _FakeClient((342.0, 616.0, None))
    dec = BrowserActionDecision(action=BrowserAction(
        action_type="tap",
        x=342,
        y=616,
        description="Click the 'Add Swatch' button.",
    ))

    assert _exec(c).execute(dec, target_control="Add Swatch") is True
    assert c.seen_target == "Add Swatch"
    assert c.clicked == (342, 616)


def test_execute_records_dom_snap_normalized():
    # viewport 1000x1000 → pixels == normalized, easy to assert
    c = _FakeClient((635.0, 620.0, "div 666x28"), viewport=(1000, 1000))
    dec = BrowserActionDecision(action=BrowserAction(action_type="tap", x=342, y=616, description="点选项"))
    _exec(c).execute(dec)
    snap = dec.action.snap
    assert snap is not None and snap["method"] == "dom"
    assert snap["original"] == [342, 616]            # LLM's normalized coords
    assert snap["snapped"] == [635.0, 620.0]         # DOM center, normalized back
    assert c.clicked == (635.0, 620.0)


def test_execute_no_snap_leaves_action_snap_none():
    c = _FakeClient((342.0, 616.0, None), viewport=(1000, 1000))
    dec = BrowserActionDecision(action=BrowserAction(action_type="tap", x=342, y=616, description="点"))
    _exec(c).execute(dec)
    assert dec.action.snap is None


def test_action_description_does_not_create_a_target():
    description = (
        "Click 'CATALOG' in the sidebar, then click 'Products' "
        "to navigate to the Products List page."
    )

    c = _FakeClient((-46.0, 114.0, "text 238x44"), viewport=(1281, 963))
    dec = BrowserActionDecision(action=BrowserAction(
        action_type="tap",
        x=32,
        y=240,
        description=description,
    ))

    _exec(c).execute(dec)

    assert c.seen_target == ""
    assert c.clicked is not None
    assert 40 <= c.clicked[0] <= 42
    assert 230 <= c.clicked[1] <= 232
    assert dec.action.snap is None


def test_executor_rejects_any_dom_snap_coordinate_outside_viewport():
    c = _FakeClient((-46.0, 114.0, "text 238x44"), viewport=(1281, 963))
    dec = BrowserActionDecision(action=BrowserAction(
        action_type="tap",
        x=32,
        y=240,
        description="Click the 'Products' link.",
    ))

    _exec(c).execute(dec)

    assert c.clicked is not None
    assert 40 <= c.clicked[0] <= 42
    assert 230 <= c.clicked[1] <= 232
    assert dec.action.snap is None


def test_typed_row_identity_is_passed_without_parsing_instruction():
    class StructurallyGuardedClient(_FakeClient):
        def dom_snap(self, x, y, target_text=""):
            self.seen_target = target_text
            # The device may receive the business value, but its DOM-semantic candidate filter
            # does not allow a filter input's current value or ordinary cell text to match it.
            return x, y, None

    description = "Click the 'size' row in the grid to open the Edit Attribute form."
    c = StructurallyGuardedClient(None, viewport=(1281, 963))
    dec = BrowserActionDecision(action=BrowserAction(
        action_type="tap",
        x=114,
        y=452,
        description=description,
    ))

    _exec(c).execute(dec, target_control="size")

    assert c.seen_target == "size"
    assert c.clicked is not None
    assert 145 <= c.clicked[0] <= 147
    assert 434 <= c.clicked[1] <= 436
    assert dec.action.snap is None


def test_execute_passes_typed_target_to_dom_snap():
    c = _FakeClient((966.0, 210.0, "text 44x28"), viewport=(1000, 1000))
    dec = BrowserActionDecision(action=BrowserAction(
        action_type="tap", x=967, y=245,
        description="点击虚拟机器人列表中「lucas-10002」所在行操作列下拉菜单中的「操作」",
    ))
    _exec(c).execute(dec, target_control="操作")
    assert c.seen_target == "操作"            # 标签传到了 dom_snap
    assert c.clicked == (966.0, 210.0)        # 点击落在重定向后的位置
    assert dec.action.snap is not None and "text" in dec.action.snap["info"]


def test_execute_passes_typed_menu_target_to_dom_snap():
    c = _FakeClient((222.0, 114.0, "text 238x44"), viewport=(1000, 1000))
    dec = BrowserActionDecision(action=BrowserAction(
        action_type="tap", x=175, y=260,
        description="点击左侧导航栏 Sales 菜单下的 Orders 选项",
    ))
    _exec(c).execute(dec, target_control="Orders")
    assert c.seen_target == "Orders"
    assert c.clicked == (222.0, 114.0)


def test_icon_click_rejects_snap_back_to_input_value():
    # WebArena task 113 run 20260622_165248 T10: the model aimed at the search icon,
    # but text retarget/snap moved the click to the keyword input center because the
    # input value was 'Olivia'. Icon clicks should not be pulled back into inputs.
    c = _FakeClient((313.0, 310.0, "text 391x33"), viewport=(1281, 963))
    dec = BrowserActionDecision(action=BrowserAction(
        action_type="tap", x=371, y=320,
        description="点击搜索框右侧的放大镜图标以应用'Olivia'筛选条件",
    ))
    _exec(c).execute(dec)
    assert c.seen_target == ""
    assert c.clicked is not None
    assert 470 <= c.clicked[0] <= 480
    assert 303 <= c.clicked[1] <= 313
    assert dec.action.snap is None


def test_text_retarget_radius_covers_sidebar_menu_row_misses():
    from gui_agent.adapters.browser.device import TEXT_RETARGET_RADIUS_PX

    # WebArena task 64: the vision model aimed for Sales > Orders but landed several rows
    # lower on Credit Memos/Shipments. The exact quoted label retarget must cover that
    # vertical menu distance; the old 80px radius was too small.
    assert TEXT_RETARGET_RADIUS_PX >= 160
    assert TEXT_RETARGET_RADIUS_PX <= 260


def test_type_focus_tap_uses_no_inferred_target():
    c = _FakeClient((250.0, 487.0, "text 302x20"), viewport=(1000, 1000))
    ex = _exec(c)
    ex._cur_action = BrowserAction(
        action_type="type", x=248, y=560, text="admin",
        description="在密码输入框中输入 'admin'",
    )
    ex._tap(248, 560)
    assert c.seen_target == ""


def test_type_uses_typed_field_identity_for_dom_snap():
    c = _FakeClient((565.0, 409.0, "text 120x24"), viewport=(1000, 1000))
    ex = _exec(c)
    ex._cur_action = BrowserAction(
        action_type="type", x=656, y=420, text="3",
        description="在 Quantity to 输入框填入 3",
    )
    ex._cur_target_control = "Quantity to"
    ex._tap(656, 420)
    assert c.seen_target == "Quantity to"
    assert c.clicked == (565.0, 409.0)
