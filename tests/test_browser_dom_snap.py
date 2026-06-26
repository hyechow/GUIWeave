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


# ── Text retarget (the OCR-snap analogue; regression 20260612_114219 误点删除×3) ──

def test_quoted_label_extraction():
    from gui_agent.adapters.browser.executor import _quoted_label

    # 最后一个短引号标签是动作目标(前面的引号是上下文)
    assert _quoted_label("点击「lucas-10002」所在行下拉菜单中的「操作」") == "操作"
    assert _quoted_label("点击弹窗中的「取消」按钮") == "取消"
    assert _quoted_label("点击'编辑'链接") == "编辑"
    # 超过 8 字的引号内容不当标签(下拉选项长值等,本就唯一,不需要文本重定向)
    assert _quoted_label("点击下拉列表中的'交管测试专用地图_1楼'选项") == ""
    assert _quoted_label("没有引号的指令") == ""


def test_target_label_extracts_unquoted_browser_menu_labels():
    from gui_agent.adapters.browser.executor import _target_label

    assert _target_label("点击左侧导航栏 Sales 菜单下的 Orders 选项") == "Orders"
    assert _target_label("点击左侧导航栏中的 Sales 菜单") == "Sales"
    assert _target_label("点击 Filters 按钮以展开筛选条件区域") == "Filters"
    assert _target_label("点击弹窗中的「取消」按钮") == "取消"
    assert _target_label("点击虚拟机器人列表中「lucas-10002」所在行操作列下拉菜单中的「操作」") == "操作"
    assert _target_label("点击左侧导航栏的销售菜单") == ""


def test_target_label_does_not_treat_filter_value_as_click_label():
    from gui_agent.adapters.browser.executor import _target_label

    assert _target_label("点击搜索框右侧的放大镜图标以应用'Olivia'筛选条件") == ""
    assert _target_label("点击搜索按钮搜索'Olivia'") == ""


def test_execute_passes_quoted_label_to_dom_snap():
    c = _FakeClient((966.0, 210.0, "text 44x28"), viewport=(1000, 1000))
    dec = BrowserActionDecision(action=BrowserAction(
        action_type="tap", x=967, y=245,
        description="点击虚拟机器人列表中「lucas-10002」所在行操作列下拉菜单中的「操作」",
    ))
    _exec(c).execute(dec)
    assert c.seen_target == "操作"            # 标签传到了 dom_snap
    assert c.clicked == (966.0, 210.0)        # 点击落在重定向后的位置
    assert dec.action.snap is not None and "text" in dec.action.snap["info"]


def test_execute_passes_unquoted_menu_label_to_dom_snap():
    c = _FakeClient((222.0, 114.0, "text 238x44"), viewport=(1000, 1000))
    dec = BrowserActionDecision(action=BrowserAction(
        action_type="tap", x=175, y=260,
        description="点击左侧导航栏 Sales 菜单下的 Orders 选项",
    ))
    _exec(c).execute(dec)
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


def test_type_focus_tap_does_not_text_retarget():
    # A `type` focus-tap must NOT pass the quoted value as a label: dom_snap matches inputs by
    # .value, so typing 'admin' would retarget onto an already-filled account field whose value
    # is 'admin' (run 20260613_193023: password box never filled → login stuck). Only genuine
    # tap/click should text-retarget; for a PLAIN type (no from/to/min/max qualifier) we pass
    # target_text="" → plain elementFromPoint.
    c = _FakeClient((250.0, 487.0, "text 302x20"), viewport=(1000, 1000))
    ex = _exec(c)
    ex._cur_action = BrowserAction(
        action_type="type", x=248, y=560, text="admin",
        description="在密码输入框中输入 'admin'",
    )
    ex._tap(248, 560)
    assert c.seen_target == ""                 # 'admin' 没被当标签传给 dom_snap


# ── Range-filter From/To fill: type focus-tap retargets by FIELD LABEL, not pixel ──
# A numeric range filter renders two visually-identical adjacent inputs (qty[from]/qty[to]);
# vision returns near-identical coords for both, so the focus-tap collapses onto one box
# (WebArena 186: "fill To" landed in From). The fix: pass the planner-named field label so
# dom_snap disambiguates by DOM identity + from/to role. The value stays in action.text.

def test_range_field_label_extraction():
    from gui_agent.adapters.browser.executor import _range_field_label

    assert _range_field_label("在 Quantity to 输入框填入 3") == "Quantity to"
    assert _range_field_label("把 Quantity from 设为 2") == "Quantity from"
    assert _range_field_label("Price max 设置为 100") == "Price max"
    assert _range_field_label("Weight min 填 0.5") == "Weight min"
    assert _range_field_label("set Quantity to 5") == "Quantity to"   # 动词不被吞进 label
    # 普通 type:无 from/to/min/max 限定词 → 不重定向(尤其 'admin' 的尾 'min' 不算)
    assert _range_field_label("在搜索框输入 admin") == ""
    assert _range_field_label("输入 tomato 到搜索框") == ""
    assert _range_field_label("点击 Filters 按钮") == ""


def test_type_range_fill_passes_field_label_to_dom_snap():
    # The focus-tap for a range fill carries the field label so dom_snap can pick the right
    # of two adjacent From/To inputs by identity — NOT the typed value (which stays in text).
    c = _FakeClient((565.0, 409.0, "text 120x24"), viewport=(1000, 1000))
    ex = _exec(c)
    ex._cur_action = BrowserAction(
        action_type="type", x=656, y=420, text="3",
        description="在 Quantity to 输入框填入 3",
    )
    ex._tap(656, 420)
    assert c.seen_target == "Quantity to"      # 字段标签传给 dom_snap
    assert c.clicked == (565.0, 409.0)         # 落在身份重定向后的 To 框


def test_postprocess_carries_range_fill_instruction_into_description():
    # policies._postprocess: a range-fill instruction names the field only in the planner
    # instruction (the vision description is just "执行type并输入3"); carry it into the type
    # action's description so the executor can extract the field label.
    from gui_agent.adapters.browser.executor import _range_field_label
    from gui_agent.adapters.browser.policies import BrowserActionPolicy

    pol = BrowserActionPolicy.__new__(BrowserActionPolicy)
    act = BrowserAction(action_type="type", x=656, y=420, text="3", description="执行type并输入3")
    out = pol._postprocess(BrowserActionDecision(action=act), "在 Quantity to 输入框填入 3")
    assert out.action.action_type == "type"                       # 不改动作类型
    assert _range_field_label(out.action.description) == "Quantity to"

    # 普通 type 指令不被改写(description 原样保留)
    act2 = BrowserAction(action_type="type", x=300, y=120, text="admin", description="在搜索框输入admin")
    out2 = pol._postprocess(BrowserActionDecision(action=act2), "在搜索框输入 admin")
    assert out2.action.description == "在搜索框输入admin"
