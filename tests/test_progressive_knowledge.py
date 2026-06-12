"""ProgressiveKnowledge: manifest + on-demand section selection (skill-like injection).

Deterministic unit tests for the matching/selection logic that lets the per-turn planner load
only the section(s) the checker flagged, instead of the whole elements blob.
"""

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
    assert empty.select(["如何创建用户"]) == ""




def test_exact_match():
    sel = _pk().select(["如何创建用户"])
    assert "创建用户正文" in sel
    assert "订单正文" not in sel


def test_fuzzy_match_ignores_space_and_underscore():
    # checker 把名字讲成无空格无下划线,仍要命中文件名 "如何访问Robo_Team"
    sel = _pk().select(["如何访问RoboTeam"])
    assert "访问正文" in sel


def test_page_identity_fallback():
    sel = _pk().select([], page_identity="如何查询订单的执行状态")
    assert "订单正文" in sel


def test_dedupe_name_and_page_identity():
    sel = _pk().select(["如何创建用户"], page_identity="如何创建用户")
    assert sel.count("创建用户正文") == 1


def test_caps_at_three():
    sel = _pk().select(["页面A", "页面B", "页面C", "页面D"])
    bodies = [b for b in ("a", "b", "c", "d") if f"\n{b}" in sel or sel.endswith(b)]
    assert len(bodies) == 3        # 第 4 个被截掉


def test_no_match_returns_empty():
    assert _pk().select(["不存在的页面XYZ"]) == ""


def test_pick_returns_matched_stems_for_logging():
    # pick() is the source of truth the runtime LOGS as turns[].sections_loaded — it must
    # return the resolved FILE STEMS (not the checker's paraphrase), capped + deduped.
    pk = _pk()
    assert pk.pick(["如何访问RoboTeam"]) == ["如何访问Robo_Team"]   # fuzzy → canonical stem
    assert pk.pick([], page_identity="如何查询订单的执行状态") == ["如何查询订单的执行状态"]
    assert pk.pick(["如何创建用户"], page_identity="如何创建用户") == ["如何创建用户"]   # deduped
    assert pk.pick(["页面A", "页面B", "页面C", "页面D"]) == ["页面A", "页面B", "页面C"]  # capped at 3
    assert pk.pick(["不存在XYZ"]) == []


def test_bodies_round_trips_with_pick():
    # select() == bodies(pick(...)) — the split must not change the injected text.
    pk = _pk()
    names = ["如何创建用户", "如何查询订单的执行状态"]
    assert pk.bodies(pk.pick(names)) == pk.select(names)
    assert pk.bodies(["不存在的stem"]) == ""   # unknown stem is skipped, never KeyErrors


def test_frontmatter_when_parsed_and_stripped():
    from gui_agent.core.self_learning.progressive import split_frontmatter

    meta, body = split_frontmatter("---\nwhen: 创建/启用虚拟机器人（模拟器）时\n---\n# 标题\n正文")
    assert meta == {"when": "创建/启用虚拟机器人（模拟器）时"}
    assert body == "# 标题\n正文"
    # 无 frontmatter → 原样返回
    assert split_frontmatter("# 直接正文") == ({}, "# 直接正文")


def test_when_feeds_manifest_and_body_is_clean():
    pk = ProgressiveKnowledge({
        "如何使用机器人模拟器": "---\nwhen: 创建/启用虚拟机器人（模拟器）、无硬件调试时\n---\n模拟器正文",
        "如何添加机器人": "---\nwhen: 注册/接入真实物理机器人设备时\n---\n添加正文",
        "无when的章节": "裸正文",
    })
    m = pk.selector_manifest()
    # 带 when 的行: [sid] 标题 — when（同物异名桥接的载体）
    assert "如何使用机器人模拟器 — 创建/启用虚拟机器人（模拟器）、无硬件调试时" in m
    assert "如何添加机器人 — 注册/接入真实物理机器人设备时" in m
    # 无 when 的退化为纯标题行（不带悬空的 —）
    assert "[s03] 无when的章节" in m and "无when的章节 —" not in m
    # 喂 planner 的正文必须已剥离 frontmatter
    assert pk.bodies(["如何使用机器人模拟器"]).endswith("模拟器正文")
    assert "when:" not in pk.bodies(["如何使用机器人模拟器"])


def test_selector_manifest_lists_ids_and_titles():
    m = _pk().selector_manifest()
    lines = m.splitlines()
    assert len(lines) == len(_SECTIONS)
    assert lines[0].startswith("[s01] ")
    assert "[s02] 如何访问Robo Team" in m     # underscores display as spaces
    assert "如何访问Robo_Team" not in m


def test_by_ids_exact_lookup_and_echo_variants():
    pk = _pk()
    # ids resolve positionally (enumeration order of the sections dict)
    assert pk.by_ids(["s01"]) == ["如何创建用户"]
    # tolerate echo variants: uppercase / brackets / whitespace
    assert pk.by_ids(["S02", " [s03] "]) == ["如何访问Robo_Team", "如何查询订单的执行状态"]
    # unknown ids dropped silently; dedupe; cap at 3
    assert pk.by_ids(["s99", "s01", "s01", "s02", "s03", "s04"]) == [
        "如何创建用户", "如何访问Robo_Team", "如何查询订单的执行状态",
    ]
    assert pk.by_ids([]) == [] and pk.by_ids(None) == []
