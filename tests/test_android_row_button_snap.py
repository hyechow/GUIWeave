"""Offline tests for the a11y row-button tap snap (AndroidExecutor).

The snap corrects '<user> 行 … Add/Remove 按钮' taps to the matching UIAutomator
node center, because vision confuses adjacent Mastodon member rows. We test the two
pure pieces: the trigger regex (_ROW_BUTTON_RE) and the coordinate resolver
(_row_button_coords, fed a mock UIAutomator dump). No device needed.
"""

from __future__ import annotations

from gui_agent.adapters.android.executor import _ROW_BUTTON_RE, _row_button_coords


# ── _ROW_BUTTON_RE: which instruction shapes trigger the snap ────────────────

def test_row_button_re_matches_varied_wording():
    cases = [
        ("点击 'openCompany' 所在行的 'Add' 按钮", "opencompany", "add"),
        ("点击 openCompany 所在行的 Add 按钮", "opencompany", "add"),
        ("openCompany 行右侧的 Remove 按钮", "opencompany", "remove"),
        ("openCompany 这一行的 Add 按钮", "opencompany", "add"),
        ("openCompany 那一行的 Remove 按钮", "opencompany", "remove"),
        ("pupper 行右侧 Add 按钮", "pupper", "add"),
        ("openCompany (@openCompany) 行右侧的 Add 按钮", "opencompany", "add"),
        ("点击 openCompany 用户行右侧的 'Add' 按钮", "opencompany", "add"),
        ("openUniversity 用户行的 Add 按钮", "openuniversity", "add"),
        ("openCompany 个行的 Add 按钮", "opencompany", "add"),
    ]
    for desc, want_target, want_btn in cases:
        m = _ROW_BUTTON_RE.search(desc)
        assert m is not None, f"should match: {desc!r}"
        assert m.group(1).lower() == want_target, f"{desc!r}: target {m.group(1)!r}"
        assert m.group(2).lower() == want_btn, f"{desc!r}: button {m.group(2)!r}"


def test_row_button_re_rejects_non_row_button():
    # 无 "行" 定位 / 不是 Add/Remove 按钮 → 不触发 snap，避免误伤普通 tap
    for desc in [
        "点击 Add 按钮",                  # 无 username + 行
        "点击 'openCompany' 的 Add 按钮",  # 无 "行"
        "点击 'openCompany' 所在行的 详情",  # 不是 Add/Remove
        "点击右上角的成员图标",
        "返回上一页",
    ]:
        assert _ROW_BUTTON_RE.search(desc) is None, f"should NOT match: {desc!r}"


# ── _row_button_coords: parse a mock UIAutomator dump ─────────────────────────
# Simulates a Mastodon "Search among people you follow" page: each row has a
# username label on the left and an Add button on the right, rows stacked vertically.

MOBILE_XML = """
<hierarchy><node>
  <node text="openCompany" bounds="[40,160][300,200]"/>
  <node text="Add" bounds="[830,165][910,200]"/>
  <node text="openUniversity" bounds="[40,540][300,580]"/>
  <node text="Add" bounds="[830,545][910,580]"/>
  <node text="alice" bounds="[40,700][300,740]"/>
  <node text="Add" bounds="[830,705][910,740]"/>
</node></hierarchy>
""".strip()


def test_row_button_coords_picks_target_row_not_neighbor():
    # openCompany's Add is at y≈182; must return THIS row's button, not openUniversity(≈562) or alice(≈722)
    nx, ny = _row_button_coords(MOBILE_XML, "opencompany", "add", 1080, 2400)
    # Add button [830,165][910,200] → center (870, 182.5) → normalized to 0-1000
    assert abs(nx - 870 / 1080 * 1000) < 0.01
    assert abs(ny - 182.5 / 2400 * 1000) < 0.01
    assert ny < 100  # 归一化 y ≈ 76，远低于邻行 → 没点错


def test_row_button_coords_second_row():
    nx, ny = _row_button_coords(MOBILE_XML, "openuniversity", "add", 1080, 2400)
    assert abs(ny - 562.5 / 2400 * 1000) < 0.01  # ≈ 234


def test_row_button_coords_missing_target_returns_none():
    assert _row_button_coords(MOBILE_XML, "nobody", "add", 1080, 2400) is None


def test_row_button_coords_wrong_button_returns_none():
    # page only has Add buttons, no Remove
    assert _row_button_coords(MOBILE_XML, "opencompany", "remove", 1080, 2400) is None


def test_row_button_coords_at_handle_in_content_desc():
    # username may appear as @handle in content-desc rather than text
    xml = (
        '<hierarchy><node>'
        '<node content-desc="@kitty" bounds="[40,160][300,200]"/>'
        '<node text="Add" bounds="[830,165][910,200]"/>'
        '</node></hierarchy>'
    )
    _nx, ny = _row_button_coords(xml, "kitty", "add", 1080, 2400)
    assert ny < 100
