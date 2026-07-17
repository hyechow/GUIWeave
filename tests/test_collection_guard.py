"""Collection stays agentic while coverage and traversal budgets remain hard guards."""

from __future__ import annotations

from gui_agent.core.schemas import ActionIntent

import io

from PIL import Image

from gui_agent.core.schemas import (
    Action,
    ActionDecision,
    Observation,
    PolicyTurn,
    StatementContract,
    SupervisorStep,
)
from gui_agent.core.supervisor.statement import StatementSupervisorPolicy
from gui_agent.core.supervisor.statement.schemas import (
    _StatementTransitionResult,
    _TransitionAction,
    _TransitionAssessment,
    _TransitionEvidence,
)


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 80), (200, 200, 200)).save(buf, format="PNG")
    return buf.getvalue()


def _make_policy() -> tuple[StatementSupervisorPolicy, StatementContract]:
    policy = StatementSupervisorPolicy()
    statement = StatementContract(
        id="5",
        name="采集账单明细",
        description="滚动采集指定区间的账单明细",
        success_condition="账单列表已滚动至物理底部",
        kind="collection",
        completion_strategy="scroll_until_boundary",
    )
    policy.begin_statement(statement, instance_id="test:collection", task_type="analysis")
    return policy, statement


def _scroll_turn(*, read_added: bool = False) -> PolicyTurn:
    return PolicyTurn(
        index=1,
        observation_source="eval",
        statement_instance_id="test:collection",
        supervisor=SupervisorStep(action_intent=ActionIntent(instruction='向上滚动', role='iterate', family='iterate'), summary='', statement_id='5', execution_scope='test:collection/statement'),
        action_decision=ActionDecision(
            action=Action(
                action_type="scroll",
                direction="up",
                target_area="main_content",
                description="向上滚动",
            ),
        ),
        executed=True,
        read_added_content=read_added,
    )


def _complete() -> _StatementTransitionResult:
    return _StatementTransitionResult(
        assessment=_TransitionAssessment(
            status="satisfied",
            summary="collection boundary is reached",
            established_facts=["current viewport has no more rows"],
        ),
        kind="complete",
        reason="当前屏幕看起来已到底",
        summary="疑似边界",
        evidence=[
            _TransitionEvidence(
                source="current_observation",
                claim="当前视口看起来没有更多账单",
            )
        ],
    )


def _forward() -> _StatementTransitionResult:
    return _StatementTransitionResult(
        assessment=_TransitionAssessment(
            status="in_progress",
            summary="collection boundary is not proven",
            open_gaps=["one traversal is still required"],
        ),
        kind="act",
        reason="先真实遍历一次验证边界",
        summary="继续收集",
        action=_TransitionAction(
            instruction="继续滚动当前集合以验证边界",
            atomic_role="iterate",
            action_family="iterate",
            target_control="collection viewport",
            direction="down",
            expected_result="the next collection viewport is visible",
        ),
    )


def test_zero_traversal_complete_proposal_fails_without_hidden_replan(monkeypatch):
    policy, statement = _make_policy()
    calls = 0

    def transition(*args, **kwargs):
        nonlocal calls
        calls += 1
        return _complete()

    monkeypatch.setattr(policy, "_invoke_statement_transition", transition)
    step = policy._run_single_turn(
        statement,
        Observation(png_bytes=_png(), source="eval"),
        [],
    )

    assert calls == 1
    assert step.outcome is not None and step.outcome.phase == "exhausted"


def test_adapter_boundary_without_prior_move_is_not_authoritative(monkeypatch):
    policy, statement = _make_policy()
    monkeypatch.setattr(
        policy,
        "_invoke_statement_transition",
        lambda *args, **kwargs: _complete(),
    )

    step = policy._run_single_turn(
        statement,
        Observation(
            png_bytes=_png(),
            source="eval",
            viewport={"at_scroll_end": True},
        ),
        [],
    )

    assert step.outcome is not None and step.outcome.phase == "exhausted"


def test_adapter_boundary_after_dispatched_move_validates_llm_completion(monkeypatch):
    policy, statement = _make_policy()
    monkeypatch.setattr(
        policy,
        "_invoke_statement_transition",
        lambda *args, **kwargs: _complete(),
    )

    step = policy._run_single_turn(
        statement,
        Observation(
            png_bytes=_png(),
            source="eval",
            viewport={"at_scroll_end": True},
        ),
        [_scroll_turn(read_added=True)],
    )

    assert step.outcome is not None and step.outcome.phase == "completed"


def test_collection_strategy_change_is_an_ordinary_action(monkeypatch):
    policy, statement = _make_policy()
    monkeypatch.setattr(
        policy,
        "_invoke_statement_transition",
        lambda *args, **kwargs: _StatementTransitionResult(
            assessment=_TransitionAssessment(
                status="in_progress",
                summary="the last move added no rows",
                open_gaps=["another traversal tactic is required"],
                last_action_effect="no_effect",
            ),
            kind="act",
            reason="上一滚动没有新增账单，换一个滚动区域",
            summary="调整遍历策略",
            action=_TransitionAction(
                instruction="在主账单区域向上滚动一屏",
                atomic_role="iterate",
                action_family="iterate",
                target_control="main collection region",
                direction="up",
                expected_result="a different collection viewport is visible",
            ),
        ),
    )

    step = policy._run_single_turn(
        statement,
        Observation(png_bytes=_png(), source="eval"),
        [_scroll_turn()],
    )

    assert step.action_intent is not None and step.outcome is None


def test_deterministic_loading_frame_waits_without_transition(monkeypatch):
    policy, statement = _make_policy()
    monkeypatch.setattr(
        "gui_agent.core.supervisor.statement.policy.is_loading_frame",
        lambda _observation: True,
    )
    monkeypatch.setattr(
        policy,
        "_invoke_statement_transition",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("loading guard must run before Transition")
        ),
    )

    step = policy._run_single_turn(
        statement,
        Observation(png_bytes=_png(), source="eval"),
        [_scroll_turn()],
    )

    assert step.is_loading and not step.action_intent is not None and step.outcome is None
