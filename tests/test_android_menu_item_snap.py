"""Offline tests for the a11y menu/list item tap snap (AndroidExecutor).

The snap corrects '点击下拉菜单/列表里的 X 选项' taps to the matching a11y node,
because vision jitters between adjacent dropdown rows (e.g. Mastodon Home dropdown:
Lists vs Live feed vs Followed hashtags → occasionally taps the wrong one and lands
on Explore). Pure pieces: the trigger regex (_MENU_ITEM_RE) and the coordinate
resolver (_menu_item_coords, fed a mock UIAutomator dump). No device needed.
"""

from __future__ import annotations

from gui_agent.adapters.android.executor import _MENU_ITEM_RE, _menu_item_coords


# ── _MENU_ITEM_RE: which instructions trigger the snap ────────────────────────

def test_menu_item_re_matches_dropdown_wording():
    cases = [
        ("点击下拉菜单中的 'Lists' 选项", "Lists"),
        ("点击下拉菜单的 'Lists' 选项", "Lists"),
        ("点击下拉菜单里的 'Live feed'", "Live feed"),
        ("从列表中选择 'openCompany' 项", "openCompany"),
        ("点击下拉菜单中的 Lists 选项", "Lists"),  # 无引号 + 选项（run 20260630 失败措辞）
        ("点击下拉菜单顶部的 Lists 标题栏", "Lists"),  # 无引号 + 标题栏
    ]
    for desc, want in cases:
        m = _MENU_ITEM_RE.search(desc)
        assert m is not None, f"should match: {desc!r}"
        got = m.group(1) or m.group(2)
        assert got == want, f"{desc!r}: got {got!r}"


def test_menu_item_re_rejects_non_menu():
    # 没有 菜单/列表/dropdown 上下文 → 不触发（避免误伤普通 tap）
    for desc in [
        "点击 'Home' 按钮",          # 无菜单上下文
        "点击右上角的成员图标",        # 无引号目标 / 无菜单
        "返回上一页",
        "点击 'openCompany' 所在行的 'Add' 按钮",  # row-button（另一个 snap 管）
    ]:
        assert _MENU_ITEM_RE.search(desc) is None, f"should NOT match: {desc!r}"


# ── _menu_item_coords: mock Mastodon Home dropdown dump ───────────────────────
# Real bounds captured from the device (Home/Live feed/Lists/Followed hashtags).

DROPDOWN_XML = """
<hierarchy><node>
  <node text="Home" bounds="[42,300][567,447]"/>
  <node text="Live feed" bounds="[42,447][567,594]"/>
  <node text="Lists" bounds="[42,639][567,786]"/>
  <node text="Followed hashtags" bounds="[42,786][567,933]"/>
</node></hierarchy>
""".strip()


def test_menu_item_coords_picks_lists_not_neighbor():
    # Lists [42,639][567,786] -> center (304.5, 712.5); must NOT return Live feed or Followed hashtags
    nx, ny = _menu_item_coords(DROPDOWN_XML, "Lists", 1080, 2400)
    assert abs(nx - (42 + 567) / 2 / 1080 * 1000) < 0.01
    assert abs(ny - (639 + 786) / 2 / 2400 * 1000) < 0.01  # ≈ 297


def test_menu_item_coords_live_feed():
    _nx, ny = _menu_item_coords(DROPDOWN_XML, "Live feed", 1080, 2400)
    # Live feed [42,447][567,594] -> center y = 520.5 (not Lists' 712.5)
    assert abs(ny - (447 + 594) / 2 / 2400 * 1000) < 0.01  # ≈ 217


def test_menu_item_coords_case_insensitive():
    _nx, ny = _menu_item_coords(DROPDOWN_XML, "lists", 1080, 2400)
    assert ny < 350  # still the Lists row


def test_menu_item_coords_missing_returns_none():
    assert _menu_item_coords(DROPDOWN_XML, "Bookmarks", 1080, 2400) is None


def test_menu_item_coords_ambiguous_returns_none():
    # multiple matches → ambiguous → refuse to snap (let vision decide)
    xml = (
        '<hierarchy><node>'
        '<node text="Lists" bounds="[42,639][567,786]"/>'
        '<node text="Lists" bounds="[42,2000][567,2147]"/>'
        '</node></hierarchy>'
    )
    assert _menu_item_coords(xml, "Lists", 1080, 2400) is None


def test_menu_item_coords_content_desc_match():
    xml = (
        '<hierarchy><node>'
        '<node content-desc="Lists" bounds="[42,639][567,786]"/>'
        '</node></hierarchy>'
    )
    _nx, ny = _menu_item_coords(xml, "Lists", 1080, 2400)
    assert ny < 350
