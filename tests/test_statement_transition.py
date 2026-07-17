"""Transition-first schema, context, and mechanical-boundary tests."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from gui_agent.core.run.execution_signals import CompletionEvaluation
from gui_agent.core.run.statement_memory import build_memory_view
from gui_agent.core.run.statement_transition import (
    validate_completion,
    validate_evidence_references,
)
from gui_agent.core.schemas import Observation, StatementContract
from gui_agent.core.supervisor.statement.model_io import _transition_frame_block
from gui_agent.core.supervisor.statement.observation_view import build_observation_view
from gui_agent.core.supervisor.statement.policy import StatementSupervisorPolicy
from gui_agent.core.supervisor.statement.schemas import (
    _ActionDraft,
    _StatementTransitionResult,
    _TransitionAction,
    _TransitionAssessment,
    _TransitionEvidence,
)


def _statement() -> StatementContract:
    return StatementContract(
        id="s1",
        name="open products",
        description="",
        kind="navigation",
        success_condition="Products page is visible",
    )


def _assessment(status: str, *, gaps: list[str] | None = None) -> _TransitionAssessment:
    return _TransitionAssessment(
        status=status,
        summary="current statement state",
        established_facts=["current page is visible"],
        open_gaps=gaps if gaps is not None else ([] if status != "in_progress" else ["Products is not open"]),
        last_action_effect="none",
    )


def test_transition_requires_assessment_before_decision() -> None:
    with pytest.raises(ValidationError, match="assessment"):
        _StatementTransitionResult(
            kind="act",
            reason="continue",
            action=_TransitionAction(
                instruction="在当前导航区域展开 Catalog 菜单",
                action_family="activate",
                target_control="Catalog",
                expected_result="Catalog submenu becomes visible",
            ),
        )


@pytest.mark.parametrize(
    ("kind", "status"),
    [("act", "satisfied"), ("complete", "in_progress"), ("infeasible", "in_progress")],
)
def test_assessment_and_transition_kind_must_agree(kind: str, status: str) -> None:
    payload = {
        "assessment": _assessment(status),
        "kind": kind,
        "reason": "contradictory decision",
        "action": (
            _TransitionAction(
                instruction="在当前导航区域展开 Catalog 菜单",
                action_family="activate",
                target_control="Catalog",
                expected_result="submenu becomes visible",
            )
            if kind == "act"
            else None
        ),
        "evidence": (
            [_TransitionEvidence(source="current_observation", claim="current frame")]
            if kind != "act"
            else []
        ),
        "kickback": "choose another route" if kind == "infeasible" else "",
    }
    with pytest.raises(ValidationError, match="requires assessment.status"):
        _StatementTransitionResult(**payload)


def test_structured_action_owns_where_what_and_expected_change() -> None:
    action = _TransitionAction(
        instruction="click something nearby",
        atomic_role="prepare",
        action_family="activate",
        target_control="CATALOG",
        target_ref="42",
        expected_result="CATALOG submenu becomes visible",
    )
    assert action.target_control == "CATALOG"
    assert action.action_family == "activate"
    with pytest.raises(ValidationError, match="target_control"):
        _TransitionAction(
            instruction="展开当前导航区域中的目标菜单",
            action_family="activate",
            expected_result="a menu opens",
        )
    with pytest.raises(ValidationError, match="expected_result"):
        _TransitionAction(
            instruction="在左侧导航区域展开 CATALOG 菜单",
            action_family="activate",
            target_control="CATALOG",
        )


def test_materialized_action_keeps_full_visual_instruction_and_expected_result() -> None:
    instruction = (
        "在 Product Attributes 表格筛选区，点击与 Attribute Code 输入框关联的 "
        "Search 按钮以提交当前 size 筛选"
    )
    expected_result = "Product Attributes 表格刷新并显示 Attribute Code=size 的结果"
    decision = _StatementTransitionResult(
        assessment=_assessment("in_progress"),
        kind="act",
        reason="submit the populated filter",
        action=_TransitionAction(
            instruction=instruction,
            action_family="activate",
            target_control="Search",
            expected_result=expected_result,
        ),
    )

    step, rejection = StatementSupervisorPolicy()._materialize_transition_action(
        decision,
        _statement(),
        Observation(png_bytes=b"frame", source="test"),
        execution_scope="statement:s1",
    )

    assert rejection is None
    assert step is not None and step.action_intent is not None
    assert step.action_intent.instruction == instruction
    assert step.action_intent.expected_result == expected_result


def test_current_frame_ref_is_optional_for_visual_grounding() -> None:
    view = build_observation_view(
        _statement(),
        Observation(
            png_bytes=b"frame",
            source="browser",
            semantic_tree=[{
                "role": "button",
                "key": "Search",
                "ref": 42,
                "in_viewport": True,
                "point": {"x": 120, "y": 304},
            }],
        ),
        [],
    )
    plan = _ActionDraft(
        instruction="在当前局部筛选区点击 Search 按钮",
        summary="submit filter",
        action_family="activate",
        target_control="Search",
        expected_result="the local results refresh",
    )

    assert StatementSupervisorPolicy._validate_action_capability(view, plan) == ""


def test_provider_extra_action_fields_fail_instead_of_being_repaired() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        _StatementTransitionResult.model_validate({
            "assessment": {
                "status": "in_progress",
                "summary": "filter is missing",
                "open_gaps": ["filter value is not entered"],
            },
            "kind": "act",
            "reason": "enter filter",
            "action": {
                "instruction": "在筛选区域的 Attribute Code 输入框填写 size",
                "action_family": "input",
                "target_control": "Attribute Code",
                "target_value": "size",
                "expected_result": "Attribute Code shows size",
                "attribute_code": "text_input",
            },
        })


def test_terminal_requires_evidence_and_infeasible_requires_kickback() -> None:
    with pytest.raises(ValidationError, match="requires cited evidence"):
        _StatementTransitionResult(
            assessment=_assessment("satisfied"),
            kind="complete",
            reason="done",
        )
    with pytest.raises(ValidationError, match="requires kickback"):
        _StatementTransitionResult(
            assessment=_assessment("blocked"),
            kind="infeasible",
            reason="blocked",
            evidence=[_TransitionEvidence(source="current_observation", claim="no route")],
        )


def test_affordances_distinguish_menu_activation_from_real_navigation() -> None:
    observation = Observation(
        png_bytes=b"frame",
        source="browser",
        url="https://shop.test/edit/#",
        semantic_tree=[
            {
                "role": "link",
                "key": "CATALOG",
                "ref": 10,
                "url": "https://shop.test/edit/#",
                "in_viewport": True,
            },
            {
                "role": "link",
                "key": "Products",
                "ref": 11,
                "url": "https://shop.test/products",
                "in_viewport": True,
            },
            {
                "role": "button",
                "key": "Save",
                "ref": 12,
                "in_viewport": False,
            },
        ],
    )
    view = build_observation_view(_statement(), observation, [])
    by_label = {item["label"]: item for item in view.affordances}
    assert by_label["CATALOG"]["supported_operations"] == ["activate"]
    assert by_label["Products"]["supported_operations"] == ["activate", "navigate"]
    assert by_label["Save"]["supported_operations"] == ["iterate"]


def test_transition_frame_contains_facts_and_capabilities_without_runtime_verdicts() -> None:
    statement = _statement()
    observation = Observation(
        png_bytes=b"frame",
        source="browser",
        title="Dashboard",
        url="https://shop.test/admin",
        semantic_tree=[{
            "role": "link",
            "key": "CATALOG",
            "ref": 10,
            "url": "https://shop.test/admin/#",
            "in_viewport": True,
        }],
    )
    memory = build_memory_view(
        instance_id="run:s1",
        contract=statement,
        history=[],
        observation=observation,
    )
    view = build_observation_view(statement, observation, [])
    block = _transition_frame_block(
        statement,
        observation,
        memory,
        view,
        initial_filters=None,
    )
    payload = json.loads(block.content.split("\n", 2)[2])
    assert payload["contract"]["id"] == "s1"
    assert payload["memory"]["last_action_result"] == "none"
    assert payload["observation"]["affordances"][0]["supported_operations"] == ["activate"]
    assert "allowed_transition_kinds" not in block.content
    assert "repeated_write_forbidden" not in block.content
    assert "evidence.status" not in block.content


def test_journal_evidence_reference_must_be_exposed_by_memory() -> None:
    evidence = [
        _TransitionEvidence(source="journal", event_ref="turn:4", claim="commit dispatched")
    ]
    assert validate_evidence_references(evidence, available_refs={"turn:4"}).allowed
    assert not validate_evidence_references(evidence, available_refs={"turn:3"}).allowed


def test_completion_validator_only_checks_terminal_evidence_grade() -> None:
    assert validate_completion(CompletionEvaluation("satisfied", "done", "confirmed")).allowed
    assert not validate_completion(CompletionEvaluation("pending", "not yet")).allowed
