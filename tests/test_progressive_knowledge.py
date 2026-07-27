"""ProgressiveKnowledge deterministic section retrieval for Transition."""

from __future__ import annotations

from gui_agent.core.self_learning.progressive import ProgressiveKnowledge

_SECTIONS = {
    "如何创建用户": "## 如何创建用户\n创建用户正文",
    "如何访问Robo_Team": "## 如何访问\n访问正文",      # 文件名带下划线(标题里的空格/标点)
    "如何查询订单的执行状态": "## 订单状态\n订单正文",
    "页面A": "a", "页面B": "b", "页面C": "c", "页面D": "d",
}


def _pk() -> ProgressiveKnowledge:
    return ProgressiveKnowledge(dict(_SECTIONS))


def test_bool_and_empty():
    assert bool(_pk()) is True
    empty = ProgressiveKnowledge({})
    assert bool(empty) is False
    assert empty.match_signals(["如何创建用户"]) == []


def test_exact_match():
    pk = _pk()
    sel = pk.bodies(pk.match_signals(["如何创建用户"]))
    assert "创建用户正文" in sel
    assert "订单正文" not in sel


def test_fuzzy_match_ignores_space_and_underscore():
    pk = _pk()
    sel = pk.bodies(pk.match_signals(["如何访问RoboTeam"]))
    assert "访问正文" in sel


def test_page_identity_signal():
    pk = _pk()
    sel = pk.bodies(pk.match_signals(["如何查询订单的执行状态"]))
    assert "订单正文" in sel


def test_dedupes_repeated_signals():
    pk = _pk()
    sel = pk.bodies(pk.match_signals(["如何创建用户", "如何创建用户"]))
    assert sel.count("创建用户正文") == 1


def test_caps_at_three():
    pk = _pk()
    sel = pk.bodies(pk.match_signals(["页面A", "页面B", "页面C", "页面D"]))
    bodies = [b for b in ("a", "b", "c", "d") if f"\n{b}" in sel or sel.endswith(b)]
    assert len(bodies) == 3        # 第 4 个被截掉


def test_no_match_returns_empty():
    pk = _pk()
    assert pk.match_signals(["不存在的页面XYZ"]) == []
    assert pk.bodies(pk.match_signals(["不存在的页面XYZ"])) == ""


def test_match_signals_title_then_when_fallback():
    # Title substring match first, then selector_when token/bigram overlap.
    pk = ProgressiveKnowledge({
        "如何创建订单": "---\nselector_when: 新建订单/下单时\n---\n创建正文",
        "如何查询订单执行状态": "---\nselector_when: 查询订单执行状态时\n---\n状态正文",
        "无关章节": "裸正文",
    })
    # 标题子串直接命中(最强信号),排最前
    assert pk.match_signals(["如何创建订单"])[0] == "如何创建订单"
    # 标题不命中,但 statement 文字与 when 行 bigram 重叠 → 兜底命中,按重叠度排序
    assert pk.match_signals(["某页", "新建一个订单", "订单创建成功"])[0] == "如何创建订单"
    # 完全无关 → 空;空/空白信号 → 空
    assert pk.match_signals(["今天的天气"]) == []
    assert pk.match_signals([]) == []
    assert pk.match_signals(["", "  "]) == []


def test_when_matching_ignores_stopwords_and_prefers_distinctive_terms():
    pk = ProgressiveKnowledge({
        "Reviews": "---\nselector_when: product reviews rating stars\n---\n评论正文",
        "Workspace": "---\nselector_when: product description price\n---\n商品正文",
        "Orders": "---\nselector_when: number of customer orders\n---\n订单正文",
    })

    assert pk.match_signals(["Change the page title of Home Page"]) == []
    assert pk.match_signals([
        "Update product description using the number of reviews with four stars",
    ])[:2] == ["Reviews", "Workspace"]
    assert pk.match_signals(["Update a price rule"], min_overlap=2) == []
    assert pk.match_signals(["Update product description"], min_overlap=2) == ["Workspace"]


def test_bodies_render_selected_stems():
    pk = _pk()
    names = ["如何创建用户", "如何查询订单的执行状态"]
    text = pk.bodies(pk.match_signals(names))
    assert "创建用户正文" in text
    assert "订单正文" in text
    assert pk.bodies(["不存在的stem"]) == ""   # unknown stem is skipped, never KeyErrors


def test_frontmatter_when_parsed_and_stripped():
    from gui_agent.core.self_learning.progressive import split_frontmatter

    meta, body = split_frontmatter("---\nwhen: 创建/启用虚拟机器人（模拟器）时\n---\n# 标题\n正文")
    assert meta == {"when": "创建/启用虚拟机器人（模拟器）时"}
    assert body == "# 标题\n正文"
    # 无 frontmatter → 原样返回
    assert split_frontmatter("# 直接正文") == ({}, "# 直接正文")


def test_frontmatter_supports_context_metadata_lists():
    from gui_agent.core.self_learning.progressive import split_frontmatter

    meta, body = split_frontmatter(
        "---\n"
        "id: knowledge.browser.shopping_admin.orders\n"
        "source_type: knowledge_section\n"
        "scope:\n"
        "  - transition\n"
        "selector_when: 订单统计时\n"
        "---\n"
        "正文"
    )

    assert meta["id"] == "knowledge.browser.shopping_admin.orders"
    assert meta["scope"] == ["transition"]
    assert meta["selector_when"] == "订单统计时"
    assert body == "正文"


def test_when_feeds_deterministic_match_and_body_is_clean():
    pk = ProgressiveKnowledge({
        "如何使用机器人模拟器": "---\nwhen: 创建/启用虚拟机器人（模拟器）、无硬件调试时\n---\n模拟器正文",
        "如何添加机器人": "---\nwhen: 注册/接入真实物理机器人设备时\n---\n添加正文",
        "无when的章节": "裸正文",
    })
    assert pk.match_signals(["创建并启用虚拟机器人"])[0] == "如何使用机器人模拟器"
    # 喂 Transition 的正文必须已剥离 frontmatter
    assert pk.bodies(["如何使用机器人模拟器"]).endswith("模拟器正文")
    assert "when:" not in pk.bodies(["如何使用机器人模拟器"])


def test_selector_when_takes_precedence_for_matching_and_bodies_render_metadata():
    pk = ProgressiveKnowledge({
        "Orders": (
            "---\n"
            "id: knowledge.browser.shopping_admin.orders\n"
            "source_type: knowledge_section\n"
            "platform: browser\n"
            "app: shopping_admin\n"
            "when: legacy hint\n"
            "selector_when: completed orders 和 customer email 聚合任务\n"
            "ttl: session\n"
            "---\n"
            "订单正文"
        ),
    })

    matched = pk.match_signals(["completed orders 和 customer email 聚合任务"])
    body = pk.bodies(["Orders"])

    assert matched == ["Orders"]
    assert "context: knowledge.browser.shopping_admin.orders" in body
    assert "type=knowledge_section" in body
    assert "app=shopping_admin" in body
    assert "订单正文" in body
