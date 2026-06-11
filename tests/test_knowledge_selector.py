"""KnowledgeSelector wiring: per-(milestone, page) caching + failure fallback.

The selector is a dedicated LLM micro-decision (run_selector) picking knowledge section
ids for the planner. These tests stub the LLM call and lock the POLICY-side contract:

  - fires once per (milestone_id, normalized page_identity), then serves from cache
  - page change / milestone change → new key → fires again
  - empty selections are cached too (no per-turn retry on a no-knowledge page)
  - a selector exception falls back to the page_identity fuzzy match and is NOT cached
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


def test_empty_selection_is_cached(monkeypatch):
    p = _policy()
    calls = _stub(monkeypatch, [_SelectorResult(section_ids=[])])
    ms = _ms()
    assert p._select_sections(ms, _check("无关页")) == []
    assert p._select_sections(ms, _check("无关页")) == []
    assert calls["n"] == 1  # 空结果也缓存,不逐轮重试


def test_failure_falls_back_and_is_not_cached(monkeypatch):
    p = _policy()
    calls = _stub(monkeypatch, None, error=RuntimeError("llm down"))
    ms = _ms()
    # 失败 → page_identity 模糊兜底(命中"如何查询订单的执行状态"需页面名含其子串;此处不命中=空)
    assert p._select_sections(ms, _check("如何查询订单的执行状态")) == ["如何查询订单的执行状态"]
    assert p._select_sections(ms, _check("如何查询订单的执行状态")) == ["如何查询订单的执行状态"]
    assert calls["n"] == 2  # 未缓存,每次都重试 LLM


def test_no_progressive_knowledge_returns_empty():
    p = MilestoneSupervisorPolicy()
    p.set_app_knowledge("nav", app_name="X", elements="elements")  # 无 sections → _pk=None
    assert p._select_sections(_ms(), _check("任意页")) == []
