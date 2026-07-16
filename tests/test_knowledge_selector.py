"""Progressive knowledge retrieval is deterministic context selection, not control flow."""

from __future__ import annotations

import inspect

from gui_agent.core.self_learning.progressive import ProgressiveKnowledge
from gui_agent.core.supervisor.statement import llm_runtime
from gui_agent.core.supervisor.statement.execution_scope import page_known


def test_selector_when_matches_statement_signals() -> None:
    knowledge = ProgressiveKnowledge({
        "如何创建订单": "---\nselector_when: 新建订单/下单时\n---\n创建订单正文",
        "如何查询订单执行状态": "---\nselector_when: 查询订单执行状态时\n---\n状态正文",
    })

    stems = knowledge.match_signals(["新建一个订单", "订单创建成功"])

    assert "如何创建订单" in stems
    assert "创建订单正文" in (knowledge.bodies(stems) or "")


def test_irrelevant_signals_select_no_sections() -> None:
    knowledge = ProgressiveKnowledge({
        "如何创建订单": "---\nselector_when: 新建订单/下单时\n---\n创建订单正文",
    })

    assert knowledge.match_signals(["无关页面", "检查连通性"]) == []


def test_transition_bridge_has_no_selector_llm_or_cache_route() -> None:
    source = inspect.getsource(llm_runtime.StatementLLMRuntimeMixin)

    assert "run_selector" not in source
    assert "_select_sections" not in source
    assert "_selector_cache" not in source
    assert "match_signals" in source


def test_unknown_page_markers_are_substring_matched() -> None:
    for variant in [
        "无法识别当前页面",
        "未知页面（用户中心？）",
        "unknown page",
        "页面不确定",
        "Unidentified view",
    ]:
        assert page_known(variant) is False, variant
    for known in ["订单列表页", "个人中心", "WeChat 聊天列表"]:
        assert page_known(known) is True, known
