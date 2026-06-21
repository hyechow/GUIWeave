"""Stage 2: the supervisor's _maybe_kickback routing at give-up time (mechanism-2 hook).

The feasibility LLM is monkeypatched — these pin the deterministic ROUTING: infeasible → a stop
step carrying the directive; feasible/visual/judge-error → None (fall through to normal fail)."""

import gui_agent.core.supervisor.milestone.feasibility as feas
from gui_agent.core.schemas import Milestone, Observation
from gui_agent.core.supervisor.milestone.feasibility import FeasibilityVerdict
from gui_agent.core.supervisor.milestone.policy import MilestoneSupervisorPolicy


def _ms() -> Milestone:
    return Milestone.model_validate({
        "id": "m1", "name": "设置评分筛选", "description": "d",
        "success_condition": "Rating<=3 已应用", "kind": "filter",
    })


def _obs_browser() -> Observation:
    return Observation(
        png_bytes=b"png", source="browser",
        form_controls=[{"label": "Product", "kind": "input"}, {"label": "Nickname", "kind": "input"}],
    )


def test_kickback_when_infeasible(monkeypatch):
    monkeypatch.setattr(feas, "judge_feasibility",
                        lambda *a, **k: FeasibilityVerdict(feasible=False, reason="无 Rating 控件", directive="逐条钻取评论详情"))
    p = MilestoneSupervisorPolicy()
    ms = _ms()
    step = p._maybe_kickback(ms, _obs_browser(), None)
    assert step is not None
    assert step.stop is True
    assert step.replan_directive == "逐条钻取评论详情"
    assert "不可行" in (step.stop_reason or "")
    assert ms.status == "failed"


def test_no_kickback_when_feasible(monkeypatch):
    monkeypatch.setattr(feas, "judge_feasibility",
                        lambda *a, **k: FeasibilityVerdict(feasible=True, reason="有搜索框"))
    p = MilestoneSupervisorPolicy()
    ms = _ms()
    assert p._maybe_kickback(ms, _obs_browser(), None) is None
    assert ms.status != "failed"  # untouched → normal fail path runs


def test_no_kickback_on_visual_platform_no_dom_controls(monkeypatch):
    called = []

    def _spy(*a, **k):
        called.append(1)
        return FeasibilityVerdict(feasible=False)

    monkeypatch.setattr(feas, "judge_feasibility", _spy)
    p = MilestoneSupervisorPolicy()
    # no form_controls / ui_facts (visual platform) → control sentinel → judge NEVER called
    assert p._maybe_kickback(_ms(), Observation(png_bytes=b"png", source="iphone"), None) is None
    assert not called


def test_judge_exception_treated_as_feasible(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("llm down")

    monkeypatch.setattr(feas, "judge_feasibility", _boom)
    p = MilestoneSupervisorPolicy()
    assert p._maybe_kickback(_ms(), _obs_browser(), None) is None  # never crashes the run
