"""Stage 2: the supervisor's _maybe_kickback routing at give-up time (Feasibility Guard hook).

The feasibility LLM is monkeypatched — these pin the deterministic ROUTING: infeasible → a stop
step carrying the directive; feasible/visual/judge-error → None (fall through to normal fail)."""

import gui_agent.core.supervisor.milestone.feasibility as feas
from gui_agent.core.schemas import Milestone, Observation
from gui_agent.core.supervisor.milestone.feasibility import FeasibilityVerdict
from gui_agent.core.supervisor.milestone.policy import MilestoneSupervisorPolicy
from gui_agent.core.supervisor.milestone.runtime import EARLY_FEASIBILITY_AT, MAX_RETRIES
from gui_agent.core.supervisor.milestone.schemas import _ReplanResult, _SingleCheckResult


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


# ── Early Feasibility probe: consult the guard before the MAX_RETRIES give-up ──
def _ms_action() -> Milestone:
    return Milestone.model_validate({
        "id": "m1", "name": "筛选", "description": "d", "success_condition": "s", "kind": "action",
    })


def _stuck_check() -> _SingleCheckResult:
    return _SingleCheckResult(status="stuck", reason="打转", summary="")


def test_early_feasibility_probe_fires_at_threshold(monkeypatch):
    """A milestone stuck EARLY_FEASIBILITY_AT times gets a Feasibility probe before MAX — and again
    at MAX — but only ONCE early (not every retry)."""
    seen: list[int] = []
    p = MilestoneSupervisorPolicy()
    monkeypatch.setattr(p, "_maybe_kickback", lambda ms, obs, ri, hist=None: (seen.append(ms.retry_count), None)[1])
    monkeypatch.setattr(p, "_invoke_replanner",
                        lambda *a, **k: _ReplanResult(diagnosis="d", strategy="local_replan", instruction="x"))
    ms = _ms_action()
    obs = Observation(png_bytes=b"png", source="browser", form_controls=[{"label": "Product", "kind": "input"}])

    ms.retry_count = EARLY_FEASIBILITY_AT - 1            # → EARLY_FEASIBILITY_AT after the increment
    p._handle_stuck(ms, _stuck_check(), None, obs, [])
    assert seen == [EARLY_FEASIBILITY_AT]               # early probe fired once

    p._handle_stuck(ms, _stuck_check(), None, obs, [])  # retry → MAX_RETRIES → the give-up probe fires
    assert seen == [EARLY_FEASIBILITY_AT, MAX_RETRIES]  # NOT re-probed early (probed set)


def test_no_early_probe_below_threshold(monkeypatch):
    seen: list[int] = []
    p = MilestoneSupervisorPolicy()
    monkeypatch.setattr(p, "_maybe_kickback", lambda ms, obs, ri, hist=None: (seen.append(ms.retry_count), None)[1])
    monkeypatch.setattr(p, "_invoke_replanner",
                        lambda *a, **k: _ReplanResult(diagnosis="d", strategy="local_replan", instruction="x"))
    ms = _ms_action()
    ms.retry_count = 0                                   # → 1 after increment, below the threshold
    obs = Observation(png_bytes=b"png", source="browser", form_controls=[{"label": "Product", "kind": "input"}])
    p._handle_stuck(ms, _stuck_check(), None, obs, [])
    assert seen == []
