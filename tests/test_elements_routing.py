"""Element knowledge routing (_elements_for).

The planner and replanner both generate instructions, so both should inject the SAME
focused element knowledge: the per-section bodies the KnowledgeSelector picked for the
current (milestone, page), not the whole _elements.md blob. _elements.md remains only the
fallback for apps that have no per-section knowledge. (Decompose injects no element
knowledge at all — it only needs navigation/flow structure.)
"""

from __future__ import annotations

from gui_agent.core.schemas import Milestone
from gui_agent.core.supervisor.milestone.schemas import _SingleCheckResult
from gui_agent.core.supervisor.milestone.policy import MilestoneSupervisorPolicy


def _ms():
    return Milestone.model_validate(
        {"id": "x", "name": "配置", "description": "d", "success_condition": "s", "kind": "action"}
    )


def _check():
    return _SingleCheckResult.model_validate(
        {
            "status": "in_progress",
            "outcome_status": "unverified",
            "reason": "r",
            "summary": "s",
            "page_identity": "订单页",
        }
    )


def test_elements_for_uses_progressive_sections_when_present(monkeypatch):
    p = MilestoneSupervisorPolicy()
    p.set_app_knowledge(
        "nav", elements="FULL_ELEMENTS_BLOB",
        sections={"创建订单": "点快速建单", "连通性": "点检测"},
    )
    # stub the KnowledgeSelector micro-decision (no LLM in a unit test)
    monkeypatch.setattr(p, "_select_sections", lambda m, c: ["创建订单"])
    out = p._elements_for(_ms(), _check())
    assert out is not None
    assert "点快速建单" in out            # body of the selected section
    assert "点检测" not in out             # unselected section excluded
    assert "FULL_ELEMENTS_BLOB" not in out  # NOT the whole _elements.md blob


def test_elements_for_falls_back_to_full_blob_without_sections():
    p = MilestoneSupervisorPolicy()
    p.set_app_knowledge("nav", elements="FULL_ELEMENTS_BLOB", sections=None)
    assert p._elements_for(_ms(), _check()) == "FULL_ELEMENTS_BLOB"


def test_elements_for_none_when_no_knowledge():
    p = MilestoneSupervisorPolicy()
    p.set_app_knowledge("nav", elements="", sections=None)
    assert p._elements_for(_ms(), _check()) is None
