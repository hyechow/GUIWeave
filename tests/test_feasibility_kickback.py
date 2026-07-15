"""The supervisor's _maybe_kickback routing at give-up time.

The feasibility LLM is monkeypatched — these pin the deterministic routing:
infeasible outcome with kickback; feasible/visual/judge-error → None."""

import gui_agent.core.supervisor.statement.feasibility as feas
def _begin(p, ms):
    p.begin_statement(ms, instance_id="i1")
    return p

from gui_agent.core.schemas import StatementContract, Observation, PolicyTurn
from gui_agent.core.supervisor.statement.feasibility import FeasibilityVerdict
from gui_agent.core.supervisor.statement.policy import StatementSupervisorPolicy
from gui_agent.core.supervisor.statement.runtime import EARLY_FEASIBILITY_AT, MAX_RETRIES
from gui_agent.core.supervisor.statement.schemas import _ReplanResult, _SingleCheckResult


def _ms() -> StatementContract:
    return StatementContract.model_validate({
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
    p = StatementSupervisorPolicy()
    ms = _ms()
    p.begin_statement(ms, instance_id="i1")
    step = p._maybe_kickback(ms, _obs_browser(), None)
    assert step is not None
    assert step.outcome is not None
    assert step.outcome.phase == "infeasible"
    assert step.outcome.kickback == "逐条钻取评论详情"


def test_no_kickback_when_feasible(monkeypatch):
    monkeypatch.setattr(feas, "judge_feasibility",
                        lambda *a, **k: FeasibilityVerdict(feasible=True, reason="有搜索框"))
    p = StatementSupervisorPolicy()
    ms = _ms()
    p.begin_statement(ms, instance_id="i1")
    assert p._maybe_kickback(ms, _obs_browser(), None) is None


def test_no_kickback_on_visual_platform_no_dom_controls(monkeypatch):
    called = []

    def _spy(*a, **k):
        called.append(1)
        return FeasibilityVerdict(feasible=False)

    monkeypatch.setattr(feas, "judge_feasibility", _spy)
    p = StatementSupervisorPolicy()
    ms = _ms()
    p.begin_statement(ms, instance_id="i1")
    # no form_controls / ui_facts (visual platform) → control sentinel → judge NEVER called
    assert p._maybe_kickback(ms, Observation(png_bytes=b"png", source="iphone"), None) is None
    assert not called


def test_judge_exception_treated_as_feasible(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("llm down")

    monkeypatch.setattr(feas, "judge_feasibility", _boom)
    p = StatementSupervisorPolicy()
    ms = _ms()
    p.begin_statement(ms, instance_id="i1")
    assert p._maybe_kickback(ms, _obs_browser(), None) is None  # never crashes the run


def test_navigation_target_in_semantic_inventory_cannot_be_kicked_back(monkeypatch):
    called = []

    def _spy(*args, **kwargs):
        called.append((args, kwargs))
        return FeasibilityVerdict(feasible=False, reason="入口不存在")

    monkeypatch.setattr(feas, "judge_feasibility", _spy)
    p = StatementSupervisorPolicy()
    ms = StatementContract.model_validate({
        "id": "nav-products",
        "name": "进入目标列表页面",
        "description": "d",
        "success_condition": "页面显示目标列表",
        "kind": "navigation",
    })
    prior = PolicyTurn.model_validate({
        "index": 1,
        "observation_source": "browser",
        "supervisor": {
                "should_act": True,
                "instruction": "点击展开菜单中的目标链接",
                "summary": "",
            "statement_id": "nav-products",
            "target_control": "Products",
        },
    })
    obs = Observation(
        png_bytes=b"png",
        source="browser",
        form_controls=[{"label": "Search", "kind": "input"}],
        semantic_tree=[{"role": "link", "key": "Products", "ref": 17, "depth": 2}],
    )

    p.begin_statement(ms, instance_id="i1")
    assert p._maybe_kickback(ms, obs, None, [prior]) is None
    assert called == []


# ── Early Feasibility probe: consult the guard before the MAX_RETRIES give-up ──
def _ms_action() -> StatementContract:
    return StatementContract.model_validate({
        "id": "m1", "name": "筛选", "description": "d", "success_condition": "s", "kind": "action",
    })


def _stuck_check() -> _SingleCheckResult:
    return _SingleCheckResult(status="stuck", effect_status="unverified", reason="打转", summary="")


def test_early_feasibility_probe_fires_at_threshold(monkeypatch):
    """A statement stuck EARLY_FEASIBILITY_AT times gets a Feasibility probe before MAX — and again
    at MAX — but only ONCE early (not every retry)."""
    seen: list[int] = []
    p = StatementSupervisorPolicy()
    ms = _ms_action()
    p.begin_statement(ms, instance_id="i1")
    monkeypatch.setattr(p, "_maybe_kickback", lambda ms, obs, ri, hist=None: (seen.append(p._rt.retry_count), None)[1])
    monkeypatch.setattr(p, "_invoke_replanner",
                        lambda *a, **k: _ReplanResult(diagnosis="d", strategy="local_replan", instruction="x"))
    obs = Observation(png_bytes=b"png", source="browser", form_controls=[{"label": "Product", "kind": "input"}])

    p._rt.retry_count = EARLY_FEASIBILITY_AT - 1            # → EARLY_FEASIBILITY_AT after the increment
    p._handle_stuck(ms, _stuck_check(), None, obs, [])
    assert seen == [EARLY_FEASIBILITY_AT]               # early probe fired once

    p._handle_stuck(ms, _stuck_check(), None, obs, [])  # retry → MAX_RETRIES → the give-up probe fires
    assert seen == [EARLY_FEASIBILITY_AT, MAX_RETRIES]  # NOT re-probed early (probed set)


def test_no_early_probe_below_threshold(monkeypatch):
    seen: list[int] = []
    p = StatementSupervisorPolicy()
    ms = _ms_action()
    p.begin_statement(ms, instance_id="i1")
    monkeypatch.setattr(p, "_maybe_kickback", lambda ms, obs, ri, hist=None: (seen.append(p._rt.retry_count), None)[1])
    monkeypatch.setattr(p, "_invoke_replanner",
                        lambda *a, **k: _ReplanResult(diagnosis="d", strategy="local_replan", instruction="x"))
    p._rt.retry_count = 0                                   # → 1 after increment, below the threshold
    obs = Observation(png_bytes=b"png", source="browser", form_controls=[{"label": "Product", "kind": "input"}])
    p._handle_stuck(ms, _stuck_check(), None, obs, [])
    assert seen == []
