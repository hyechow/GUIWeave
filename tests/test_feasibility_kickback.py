"""Infeasible is a Transition proposal guarded by evidence and recovery budget."""

from __future__ import annotations

from gui_agent.core.schemas import Observation, StatementContract
from gui_agent.core.orchestrator.recovery import compose_kickback_directive
from gui_agent.core.supervisor.statement.policy import StatementSupervisorPolicy
from gui_agent.core.supervisor.statement.schemas import (
    _StatementTransitionResult,
    _TransitionAction,
    _TransitionEvidence,
)


def _statement(kind: str = "filter") -> StatementContract:
    return StatementContract(
        id="m1",
        name="设置评分筛选",
        description="应用评分条件",
        success_condition="Rating<=3 已应用",
        kind=kind,
    )


def _decision() -> _StatementTransitionResult:
    return _StatementTransitionResult(
        kind="infeasible",
        reason="完整控件清单中没有 Rating 控件",
        kickback=compose_kickback_directive(
            dead_route="继续在当前列表寻找 Rating 筛选",
            required_route="读取可见详情字段",
        ),
        evidence=[
            _TransitionEvidence(
                source="current_observation",
                claim="完整控件清单仅有 Product 和 Nickname",
            )
        ],
    )


def _act() -> _StatementTransitionResult:
    return _StatementTransitionResult(
        kind="act",
        reason="结构清单尚不完整，换一个可见入口继续找",
        action=_TransitionAction(instruction="展开筛选区域", action_family="activate"),
    )


def _policy(statement: StatementContract) -> StatementSupervisorPolicy:
    policy = StatementSupervisorPolicy()
    policy.begin_statement(statement, instance_id="i1")
    return policy


def test_complete_control_inventory_allows_infeasible_kickback(monkeypatch):
    statement = _statement()
    policy = _policy(statement)
    monkeypatch.setattr(policy, "_invoke_statement_transition", lambda *a, **k: _decision())
    monkeypatch.setattr(
        "gui_agent.core.supervisor.statement.policy.is_loading_frame",
        lambda _observation: False,
    )

    step = policy._run_single_turn(
        statement,
        Observation(
            png_bytes=b"x",
            source="browser",
            form_controls_meta={"coverage": "complete"},
            form_controls=[
                {"label": "Product", "kind": "input"},
                {"label": "Nickname", "kind": "input"},
            ],
        ),
        [],
    )

    assert step.outcome is not None and step.outcome.phase == "infeasible"
    assert "【死路｜禁止再用】" in step.outcome.kickback


def test_partial_inventory_vetoes_infeasible_then_redecides(monkeypatch):
    statement = _statement()
    policy = _policy(statement)
    decisions = iter([_decision(), _act()])
    monkeypatch.setattr(
        policy,
        "_invoke_statement_transition",
        lambda *a, **k: next(decisions),
    )
    monkeypatch.setattr(
        "gui_agent.core.supervisor.statement.policy.is_loading_frame",
        lambda _observation: False,
    )

    step = policy._run_single_turn(
        statement,
        Observation(
            png_bytes=b"x",
            source="browser",
            form_controls_meta={"coverage": "partial", "truncated": True},
            form_controls=[{"label": "Product", "kind": "input"}],
        ),
        [],
    )

    assert step.outcome is None and step.should_act


def test_visual_only_infeasible_is_vetoed_without_structural_absence(monkeypatch):
    statement = _statement(kind="action")
    policy = _policy(statement)
    monkeypatch.setattr(policy, "_invoke_statement_transition", lambda *a, **k: _decision())
    monkeypatch.setattr(
        "gui_agent.core.supervisor.statement.policy.is_loading_frame",
        lambda _observation: False,
    )

    step = policy._run_single_turn(
        statement,
        Observation(png_bytes=b"x", source="iphone"),
        [],
    )

    assert step.outcome is not None and step.outcome.phase == "exhausted"


def test_navigation_form_inventory_cannot_prove_link_absence(monkeypatch):
    statement = _statement(kind="navigation")
    policy = _policy(statement)
    decisions = iter([_decision(), _act()])
    monkeypatch.setattr(
        policy,
        "_invoke_statement_transition",
        lambda *a, **k: next(decisions),
    )
    monkeypatch.setattr(
        "gui_agent.core.supervisor.statement.policy.is_loading_frame",
        lambda _observation: False,
    )

    step = policy._run_single_turn(
        statement,
        Observation(
            png_bytes=b"x",
            source="browser",
            form_controls_meta={"coverage": "complete"},
            form_controls=[{"label": "Search", "kind": "input"}],
            semantic_tree=[{"role": "link", "key": "Products", "ref": 17}],
        ),
        [],
    )

    assert step.outcome is None and step.should_act
