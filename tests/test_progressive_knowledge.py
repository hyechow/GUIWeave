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


def test_manifest_lists_names_and_instruction():
    m = _pk().manifest_text()
    assert "relevant_sections" in m
    assert "如何创建用户" in m
    assert "如何访问Robo Team" in m            # 下划线在清单里显示为空格
    assert "如何访问Robo_Team" not in m


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
