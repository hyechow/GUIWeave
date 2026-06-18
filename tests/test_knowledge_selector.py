"""KnowledgeSelector wiring: per-(milestone, page) caching + failure fallback.

The selector is a dedicated LLM micro-decision (run_selector) picking knowledge section
ids for the planner. These tests stub the LLM call and lock the POLICY-side contract:

  - fires once per (milestone_id, normalized page_identity), then serves from cache
  - page change / milestone change → new key → fires again
  - empty selections are cached too ON A KNOWN PAGE (no per-turn retry on a no-knowledge page)
  - an UNKNOWN page (empty / 未识别) never caches an empty pick — it re-decides every turn,
    so knowledge does not go permanently dark when page identity is the weak signal
  - a clean-empty selector falls back to a deterministic match of (page id + milestone name +
    success_condition) against section titles / selector_when lines before giving up
  - a selector exception falls back to the same deterministic match and is NOT cached
    (the next turn retries the LLM)
"""

from __future__ import annotations

import gui_agent.core.supervisor.milestone.policy as policy_mod
from gui_agent.core.schemas import Milestone
from gui_agent.core.supervisor.milestone.policy import MilestoneSupervisorPolicy
from gui_agent.core.supervisor.milestone.schemas import _SelectorResult, _SingleCheckResult

_SECTIONS = {
    "如何访问Robo_Team": "访问正文",
    "如何创建订单": "创建订单正文",
    "如何查询订单的执行状态": "订单状态正文",
}


def _policy() -> MilestoneSupervisorPolicy:
    p = MilestoneSupervisorPolicy()
    p.set_app_knowledge("nav", app_name="RoboTeam", elements="elements", sections=dict(_SECTIONS))
    return p


def _ms(mid: str = "m1") -> Milestone:
    return Milestone.model_validate(
        {"id": mid, "name": "进入订单列表", "description": "d", "success_condition": "s", "kind": "navigation"}
    )


def _check(page: str) -> _SingleCheckResult:
    return _SingleCheckResult(status="in_progress", reason="r" * 10, summary="s", page_identity=page)


def _stub(monkeypatch, results=None, error: Exception | None = None):
    """Replace policy_mod.run_selector with a counting stub returning canned results."""
    calls = {"n": 0}

    def fake(goal, milestone, page_identity, manifest, *, prompts=None):
        calls["n"] += 1
        if error is not None:
            raise error
        return results[min(calls["n"], len(results)) - 1]

    monkeypatch.setattr(policy_mod, "run_selector", fake)
    return calls


def test_same_page_hits_cache(monkeypatch):
    p = _policy()
    calls = _stub(monkeypatch, [_SelectorResult(section_ids=["s01"])])
    ms = _ms()
    assert p._select_sections(ms, _check("订单列表页")) == ["如何访问Robo_Team"]
    assert p._select_sections(ms, _check("订单列表页")) == ["如何访问Robo_Team"]
    assert p._select_sections(ms, _check("订单 列表页")) == ["如何访问Robo_Team"]  # 标点/空格归一同键
    assert calls["n"] == 1


def test_page_or_milestone_change_refires(monkeypatch):
    p = _policy()
    calls = _stub(monkeypatch, [
        _SelectorResult(section_ids=["s01"]),
        _SelectorResult(section_ids=["s02"]),
        _SelectorResult(section_ids=["s03"]),
    ])
    assert p._select_sections(_ms("m1"), _check("页面A")) == ["如何访问Robo_Team"]
    assert p._select_sections(_ms("m1"), _check("页面B")) == ["如何创建订单"]        # 翻页 → 重选
    assert p._select_sections(_ms("m2"), _check("页面B")) == ["如何查询订单的执行状态"]  # 换 milestone → 重选
    assert calls["n"] == 3


def test_empty_selection_is_cached_on_known_page(monkeypatch):
    p = _policy()
    calls = _stub(monkeypatch, [_SelectorResult(section_ids=[])])
    ms = _ms()
    # "无关页" 是已识别页面 + selector 明确返空 + 兜底也无命中(这些 section 无 when 行、标题不子串命中)
    # → 缓存空结果,不逐轮重试
    assert p._select_sections(ms, _check("无关页")) == []
    assert p._select_sections(ms, _check("无关页")) == []
    assert calls["n"] == 1


def test_unknown_page_empty_is_not_cached(monkeypatch):
    p = _policy()
    calls = _stub(monkeypatch, [_SelectorResult(section_ids=[]), _SelectorResult(section_ids=[])])
    ms = _ms()
    # 页面未识别(空 / "未识别")→ 空选择不缓存,逐轮重决(避免页面识别最弱时知识永久关闭)
    assert p._select_sections(ms, _check("")) == []
    assert p._select_sections(ms, _check("未识别")) == []
    assert calls["n"] == 2


def test_unknown_page_markers_are_substring_matched(monkeypatch):
    # The checker writes free-form page identity;归一化后精确匹配会漏掉所有真实变体,必须子串判定。
    from gui_agent.core.supervisor.milestone.policy import _page_known

    for variant in ["无法识别当前页面", "未知页面（用户中心？）", "unknown page", "页面不确定", "Unidentified view"]:
        assert _page_known(variant) is False, variant
    for known in ["订单列表页", "个人中心", "WeChat 聊天列表"]:
        assert _page_known(known) is True, known

    # 端到端:一个会归一成 "无法识别当前页面" 的身份,空选择不入缓存,逐轮重决
    calls = _stub(monkeypatch, [_SelectorResult(section_ids=[]), _SelectorResult(section_ids=[])])
    p_ = _policy()
    ms = _ms()
    assert p_._select_sections(ms, _check("无法识别当前页面")) == []
    assert p_._select_sections(ms, _check("无法识别，可能是设置页")) == []
    assert calls["n"] == 2


def test_clean_empty_falls_back_to_deterministic_match(monkeypatch):
    p = MilestoneSupervisorPolicy()
    p.set_app_knowledge("nav", app_name="RoboTeam", elements="e", sections={
        "如何创建订单": "---\nselector_when: 新建订单/下单时\n---\n创建订单正文",
        "如何查询订单执行状态": "---\nselector_when: 查询订单执行状态时\n---\n状态正文",
    })
    calls = _stub(monkeypatch, [_SelectorResult(section_ids=[])])
    ms = Milestone.model_validate({
        "id": "m1", "name": "新建一个订单", "description": "d",
        "success_condition": "订单创建成功", "kind": "action",
    })
    # selector 干净返空 → 确定性兜底用 milestone 文字命中 when 行,不再永久空
    stems = p._select_sections(ms, _check("某页"))
    assert stems and stems[0] == "如何创建订单"
    assert calls["n"] == 1
    report = p._context_reports[-1]
    assert report["kind"] == "selector"
    assert report["cache"] == "miss"
    assert report["section_ids"] == []
    assert report["sections"][0] == "如何创建订单"
    assert report["fallback_triggered"] is True
    assert report["fallback_reason"] == "empty_selector"
    assert report["cached"] is True


def test_empty_page_identity_fallback_hits_but_does_not_cache(monkeypatch):
    p = MilestoneSupervisorPolicy()
    p.set_app_knowledge("nav", app_name="RoboTeam", elements="e", sections={
        "如何创建订单": "---\nselector_when: 新建订单/下单时\n---\n创建订单正文",
    })
    calls = _stub(monkeypatch, [_SelectorResult(section_ids=[]), _SelectorResult(section_ids=[])])
    ms = Milestone.model_validate({
        "id": "m1", "name": "新建一个订单", "description": "d",
        "success_condition": "订单创建成功", "kind": "action",
    })

    assert p._select_sections(ms, _check("")) == ["如何创建订单"]
    assert p._select_sections(ms, _check("")) == ["如何创建订单"]

    assert calls["n"] == 2
    assert ("m1", "") not in p._selector_cache
    first, second = p._context_reports[-2:]
    assert first["page_known"] is False
    assert first["cached"] is False
    assert first["fallback_triggered"] is True
    assert first["sections"] == ["如何创建订单"]
    assert second["cache"] == "miss"


def test_failure_falls_back_and_is_not_cached(monkeypatch):
    p = _policy()
    calls = _stub(monkeypatch, None, error=RuntimeError("llm down"))
    ms = _ms()
    # 失败 → 确定性兜底(match_signals 用 page_identity 标题子串命中"如何查询订单的执行状态")
    assert p._select_sections(ms, _check("如何查询订单的执行状态")) == ["如何查询订单的执行状态"]
    assert p._select_sections(ms, _check("如何查询订单的执行状态")) == ["如何查询订单的执行状态"]
    assert calls["n"] == 2  # 未缓存,每次都重试 LLM
    assert p._context_reports[-1]["fallback_reason"] == "selector_error"


def test_no_progressive_knowledge_returns_empty():
    p = MilestoneSupervisorPolicy()
    p.set_app_knowledge("nav", app_name="X", elements="elements")  # 无 sections → _pk=None
    assert p._select_sections(_ms(), _check("任意页")) == []
