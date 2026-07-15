"""Deterministic tests for Feasibility Guard (runtime statement-feasibility) — prompt, extraction, wiring.

The LLM judgment itself is validated offline (scripts/statement_feasibility_113.py); here we pin the
device-free parts: the prompt asset's load-bearing rules, the control-inventory extraction, and that
judge_feasibility threads the right context into the model call."""

from dataclasses import dataclass, field
from typing import Any, Optional

import gui_agent.core.supervisor.statement.feasibility as feas
from gui_agent.core.supervisor.statement.feasibility import (
    FeasibilityVerdict,
    control_presence_text,
    judge_feasibility,
)


@dataclass
class _Obs:
    form_controls: Optional[list[dict[str, Any]]] = None
    form_controls_meta: Optional[dict[str, Any]] = None
    ui_facts: Optional[list[dict[str, Any]]] = field(default=None)
    semantic_tree: Optional[list[dict[str, Any]]] = field(default=None)


def test_prompt_asset_carries_the_reliability_rules():
    p = feas._SYSTEM
    assert "直接观察" in p and "对应能力域" in p          # observation > docs, domain-correct
    assert "禁止用表单控件清单" in p                       # fields cannot disprove links
    assert "默认可行" in p                                  # feasible-default
    assert "0 条" in p or "0条" in p                        # result-count is noise
    assert "directive" in p and "禁掉【两条】死路" in p      # sharp kick-back directive


def test_control_presence_text_lists_real_controls_and_omits_absent_one():
    obs = _Obs(form_controls=[
        {"label": "Product", "kind": "input", "value": "Olivia zip jacket"},
        {"label": "Status", "kind": "native_select"},
        {"label": "Nickname", "kind": "input"},
    ])
    text = control_presence_text(obs)
    assert "Product" in text and "Nickname" in text
    assert "rating" not in text.lower() and "评分" not in text   # no rating control present


def test_control_presence_text_includes_grid_facts():
    obs = _Obs(ui_facts=[{"kind": "grid", "record_count": 0, "active_filters": ["Product: X"]}])
    text = control_presence_text(obs)
    assert "record_count=0" in text and "active_filters" in text


def test_control_presence_text_separates_navigation_inventory_from_form_controls():
    obs = _Obs(
        form_controls=[{"label": "Search", "kind": "input"}],
        semantic_tree=[
            {"role": "link", "key": "Products", "ref": 17},
            {"role": "heading", "key": "Product Attributes", "ref": 18},
        ],
    )
    text = control_presence_text(obs)
    assert "页面语义导航入口" in text
    assert "link: Products" in text
    assert "heading: Product Attributes" not in text


def test_partial_control_inventory_explicitly_disclaims_absence():
    text = control_presence_text(_Obs(
        form_controls=[{"label": "Known", "kind": "input"}],
        form_controls_meta={"coverage": "partial", "truncated": True},
    ))

    assert "部分采样" in text
    assert "不能证明" in text


def test_control_presence_text_sentinel_when_visual_only():
    assert "无适配器可感知" in control_presence_text(_Obs())  # form_controls=ui_facts=None


def test_verdict_defaults():
    v = FeasibilityVerdict(feasible=True)
    assert v.feasible is True and v.reason == "" and v.directive == ""


def test_judge_feasibility_threads_context_and_returns_verdict(monkeypatch):
    captured = {}

    def _fake_invoke(llm, messages, schema, *, trace_sink=None, trace_label=""):
        captured["system"] = messages[0].content
        captured["human"] = messages[1].content
        captured["schema"] = schema
        return FeasibilityVerdict(feasible=False, reason="无 Rating 控件", directive="逐条钻取评论详情")

    monkeypatch.setattr(feas, "invoke_structured", _fake_invoke)
    v = judge_feasibility(
        "设置 Rating<=3 筛选",
        "- Product: input\n- Nickname: input",
        knowledge="评分只在评论详情里",
        llm=object(),
    )
    assert v.feasible is False and v.directive == "逐条钻取评论详情"
    assert captured["schema"] is FeasibilityVerdict
    assert "设置 Rating<=3 筛选" in captured["human"]       # statement goal threaded
    assert "Product: input" in captured["human"]            # control inventory threaded
    assert "评分只在评论详情里" in captured["human"]         # knowledge threaded
    assert captured["system"] == feas._SYSTEM
