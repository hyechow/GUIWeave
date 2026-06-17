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
    assert _target_label("点击左侧导航栏的销售菜单") == ""


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
    # tap/click should text-retarget; for type we pass target_text="" → plain elementFromPoint.
    c = _FakeClient((250.0, 487.0, "text 302x20"), viewport=(1000, 1000))
    ex = _exec(c)
    ex._cur_action = BrowserAction(
        action_type="type", x=248, y=560, text="admin",
        description="在密码输入框中输入 'admin'",
    )
    ex._tap(248, 560)
    assert c.seen_target == ""                 # 'admin' 没被当标签传给 dom_snap
