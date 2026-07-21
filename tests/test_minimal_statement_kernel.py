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
        goal="filter attributes",
        success="Attribute Code=size is applied",
        required_values={"Attribute Code": "size"},
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


def test_offscreen_action_gets_one_same_frame_transition_retry(monkeypatch) -> None:
    statement = StatementContract(
        id="s1",
        goal="submit filter",
        success="filter submitted",
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

    assert calls == 2
    assert step.outcome is not None and step.outcome.phase == "exhausted"
    assert "does not support operation 'activate'" in step.outcome.summary
    assert policy._last_transition_record["validation_error"]


def test_wrong_target_ref_gets_one_same_frame_transition_retry(monkeypatch) -> None:
    statement = StatementContract(
        id="s1",
        goal="open products",
        success="Products visible",
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

    assert calls == 2
    assert step.outcome is not None and "target_ref" in step.outcome.summary


@pytest.mark.parametrize("target_ref", ["WACSU99", "status"])
def test_native_select_id_and_name_are_the_same_select_affordance(
    monkeypatch, target_ref
) -> None:
    statement = StatementContract(
        id="s1",
        goal="filter completed orders",
        success="only completed orders are visible",
    )
    policy = _policy(statement)
    monkeypatch.setattr(
        policy,
        "_invoke_statement_transition",
        lambda *_args, **_kwargs: _act(
            family="select",
            control="Status",
            value="Complete",
            target_ref=target_ref,
        ),
    )

    step = policy._run_single_turn(
        statement,
        _observation(form_controls=[{
            "kind": "native_select",
            "label": "notice-WACSU99",
            "name": "status",
            "id": "WACSU99",
            "options": ["Pending", "Complete"],
            "rect": {"x": 856, "y": 526, "w": 246, "h": 32},
        }]),
        [],
    )

    assert step.outcome is None
    assert step.action_intent is not None
    assert step.action_intent.family == "select"
    assert step.action_intent.target_value == "Complete"


def test_invalid_contract_write_value_fails_without_fuzzy_field_binding(monkeypatch) -> None:
    statement = StatementContract(
        id="s1",
        goal="set status",
        success="Status is Active",
        required_values={"semantic status": "Active"},
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
    assert "outside required_values" in step.outcome.summary


def test_adaptive_ui_field_name_is_allowed_when_value_matches_contract(monkeypatch) -> None:
    statement = StatementContract(
        id="s1",
        goal="set status",
        success="Status is Active",
        required_values={"semantic status": "Active"},
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


def test_nested_required_value_is_allowed_for_range_field(monkeypatch) -> None:
    statement = StatementContract(
        id="s1",
        goal="set date range",
        success="Date range is applied",
        required_values={
            "date_range": {"from": "01/01/2023", "to": "05/31/2023"},
        },
    )
    policy = _policy(statement)
    monkeypatch.setattr(
        policy,
        "_invoke_statement_transition",
        lambda *a, **k: _act(
            family="input",
            control="Purchase Date From",
            value="01/01/2023",
            role="write",
        ),
    )

    step = policy._run_single_turn(statement, _observation(), [])

    assert step.outcome is None
    assert step.action_intent is not None
    assert step.action_intent.target_value == "01/01/2023"


def test_transition_completion_is_not_reinterpreted_by_a_hidden_persistence_fsm(monkeypatch) -> None:
    statement = StatementContract(
        id="s1",
        goal="save status",
        success="Status is saved",
        persistence="explicit_commit",
        required_values={"Status": "Active"},
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
    assert step.outcome is not None and step.outcome.phase == "completed"
    assert step.pre_existing is True


def test_terminal_budget_does_not_replace_act_with_a_terminal_decision(monkeypatch) -> None:
    statement = StatementContract(
        id="s1",
        goal="continue editing",
        success="saved",
    )
    policy = _policy(statement)
    monkeypatch.setattr(policy, "_invoke_statement_transition", lambda *a, **k: _act())

    step = policy.reconcile(_observation(), "goal", [])

    assert step.outcome is not None and step.outcome.phase == "exhausted"
    assert "hard-budget final frame" in step.outcome.summary


def test_valid_completion_uses_runtime_evidence_grade(monkeypatch) -> None:
    statement = StatementContract(
        id="s1",
        goal="reach target page",
        success="Target page is visible",
    )
    policy = _policy(statement)
    monkeypatch.setattr(policy, "_invoke_statement_transition", lambda *a, **k: _complete())

    step = policy._run_single_turn(statement, _observation(), [])

    assert step.outcome is not None and step.outcome.phase == "completed"
    assert step.outcome.verification == "confirmed"
    assert policy._last_transition_record["validation_error"] == ""
