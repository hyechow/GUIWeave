"""Minimal Statement kernel: one Transition call plus mechanical validation."""

from __future__ import annotations

import io

from PIL import Image
import pytest

from gui_agent.core.schemas import Observation, StatementContract
from gui_agent.core.supervisor.statement import policy as policy_module
from gui_agent.core.supervisor.statement.policy import StatementSupervisorPolicy
from gui_agent.core.supervisor.statement.schemas import (
    _StatementTransitionResult,
    _TransitionAction,
    _TransitionAssessment,
    _TransitionEvidence,
)


INSTANCE = "run:s1"


@pytest.fixture(autouse=True)
def _frame_is_ready(monkeypatch) -> None:
    monkeypatch.setattr(policy_module, "is_loading_frame", lambda _observation: False)


def _png() -> bytes:
    stream = io.BytesIO()
    image = Image.new("RGB", (64, 64), "white")
    for x in range(32):
        for y in range(64):
            image.putpixel((x, y), (0, 0, 0))
    image.save(stream, format="PNG")
    return stream.getvalue()


def _assessment(status: str, *, gap: str = "target is not reached") -> _TransitionAssessment:
    return _TransitionAssessment(
        status=status,
        summary="current Statement state",
        established_facts=["current frame is available"],
        open_gaps=[gap] if status == "in_progress" else [],
        last_action_effect="none",
    )


def _act(
    *,
    family: str = "activate",
    control: str = "Target",
    value: str = "",
    target_ref: str = "",
    role: str = "prepare",
) -> _StatementTransitionResult:
    return _StatementTransitionResult(
        assessment=_assessment("in_progress"),
        kind="act",
        reason="the contract remains open",
        action=_TransitionAction(
            instruction=f"在当前界面中对 {control} 执行 {family}",
            atomic_role=role,
            action_family=family,
            target_control=control,
            target_value=value,
            target_ref=target_ref,
            expected_result="the target reflects the requested operation",
        ),
    )


def _complete(reason: str = "the target is visible") -> _StatementTransitionResult:
    return _StatementTransitionResult(
        assessment=_assessment("satisfied"),
        kind="complete",
        reason=reason,
        evidence=[_TransitionEvidence(source="current_observation", claim=reason)],
    )


def _policy(statement: StatementContract) -> StatementSupervisorPolicy:
    policy = StatementSupervisorPolicy()
    policy.begin_statement(statement, instance_id=INSTANCE)
    return policy


def _observation(**updates) -> Observation:
    return Observation.model_validate({
        "png_bytes": _png(),
        "source": "browser",
        "title": "Current page",
        **updates,
    })


def test_transition_preserves_complete_visual_semantic_instruction(monkeypatch) -> None:
    statement = StatementContract(
        id="s1",
        name="filter attributes",
        description="",
        kind="filter",
        success_condition="Attribute Code=size is applied",
        target_values={"Attribute Code": "size"},
    )
    policy = _policy(statement)
    decision = _act(
        family="input",
        control="Attribute Code",
        value="size",
        role="write",
    )
    instruction = (
        "在 Product Attributes 表格筛选区的 Attribute Code 输入框中填写 size，"
        "不要操作页面顶部的全局搜索框"
    )
    decision.action.instruction = instruction
    monkeypatch.setattr(policy, "_invoke_statement_transition", lambda *a, **k: decision)

    step = policy._run_single_turn(statement, _observation(), [])

    assert step.action_intent is not None
    assert step.action_intent.instruction == instruction
    assert step.action_intent.expected_result == "the target reflects the requested operation"


def test_offscreen_action_is_rejected_without_runtime_rewrite_or_retry(monkeypatch) -> None:
    statement = StatementContract(
        id="s1",
        name="submit filter",
        description="",
        kind="action",
        success_condition="filter submitted",
    )
    policy = _policy(statement)
    calls = 0

    def decide(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return _act(family="activate", control="Search", target_ref="42")

    monkeypatch.setattr(policy, "_invoke_statement_transition", decide)
    observation = _observation(semantic_tree=[{
        "role": "button",
        "key": "Search",
        "ref": 42,
        "in_viewport": False,
    }])

    step = policy._run_single_turn(statement, observation, [])

    assert calls == 1
    assert step.outcome is not None and step.outcome.phase == "exhausted"
    assert "supported=['iterate']" in step.outcome.summary
    assert policy._last_transition_record["validation_error"]


def test_wrong_target_ref_fails_once(monkeypatch) -> None:
    statement = StatementContract(
        id="s1",
        name="open products",
        description="",
        kind="navigation",
        success_condition="Products visible",
    )
    policy = _policy(statement)
    calls = 0

    def decide(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return _act(family="activate", control="Products", target_ref="99")

    monkeypatch.setattr(policy, "_invoke_statement_transition", decide)
    step = policy._run_single_turn(
        statement,
        _observation(semantic_tree=[{
            "role": "link",
            "key": "Products",
            "ref": 11,
            "in_viewport": True,
        }]),
        [],
    )

    assert calls == 1
    assert step.outcome is not None and "target_ref" in step.outcome.summary


def test_invalid_contract_write_value_fails_without_fuzzy_field_binding(monkeypatch) -> None:
    statement = StatementContract(
        id="s1",
        name="set status",
        description="",
        kind="action",
        success_condition="Status is Active",
        target_values={"semantic status": "Active"},
    )
    policy = _policy(statement)
    monkeypatch.setattr(
        policy,
        "_invoke_statement_transition",
        lambda *a, **k: _act(
            family="select",
            control="Current status",
            value="Disabled",
            role="write",
        ),
    )

    step = policy._run_single_turn(statement, _observation(), [])

    assert step.outcome is not None
    assert "allowed=['Active']" in step.outcome.summary


def test_adaptive_ui_field_name_is_allowed_when_value_matches_contract(monkeypatch) -> None:
    statement = StatementContract(
        id="s1",
        name="set status",
        description="",
        kind="action",
        success_condition="Status is Active",
        target_values={"semantic status": "Active"},
    )
    policy = _policy(statement)
    monkeypatch.setattr(
        policy,
        "_invoke_statement_transition",
        lambda *a, **k: _act(
            family="select",
            control="Current status",
            value="Active",
            role="write",
        ),
    )

    step = policy._run_single_turn(statement, _observation(), [])

    assert step.action_intent is not None
    assert step.action_intent.target_control == "Current status"


def test_false_complete_fails_once_instead_of_being_replanned(monkeypatch) -> None:
    statement = StatementContract(
        id="s1",
        name="save status",
        description="",
        kind="action",
        success_condition="Status is saved",
        persistence="explicit_commit",
        target_values={"Status": "Active"},
    )
    policy = _policy(statement)
    calls = 0

    def decide(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return _complete("Status looks Active")

    monkeypatch.setattr(policy, "_invoke_statement_transition", decide)
    step = policy._run_single_turn(statement, _observation(), [])

    assert calls == 1
    assert step.outcome is not None and step.outcome.phase == "exhausted"
    assert "validation failed" in step.outcome.summary


def test_terminal_budget_does_not_replace_act_with_a_terminal_decision(monkeypatch) -> None:
    statement = StatementContract(
        id="s1",
        name="continue editing",
        description="",
        kind="action",
        success_condition="saved",
    )
    policy = _policy(statement)
    monkeypatch.setattr(policy, "_invoke_statement_transition", lambda *a, **k: _act())

    step = policy.reconcile(_observation(), "goal", [])

    assert step.outcome is not None and step.outcome.phase == "exhausted"
    assert "hard-budget final frame" in step.outcome.summary


def test_valid_completion_uses_runtime_evidence_grade(monkeypatch) -> None:
    statement = StatementContract(
        id="s1",
        name="reach target page",
        description="",
        kind="navigation",
        success_condition="Target page is visible",
    )
    policy = _policy(statement)
    monkeypatch.setattr(policy, "_invoke_statement_transition", lambda *a, **k: _complete())

    step = policy._run_single_turn(statement, _observation(), [])

    assert step.outcome is not None and step.outcome.phase == "completed"
    assert step.outcome.verification == "accepted_unverified"
    assert policy._last_transition_record["validation_error"] == ""
