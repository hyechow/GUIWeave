"""Hard Guard for Agentic Statement Transition never chooses a fallback route."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from gui_agent.core.run.execution_signals import CompletionEvaluation
from gui_agent.core.orchestrator.recovery import compose_kickback_directive
from gui_agent.core.run.statement_transition import (
    guard_complete,
    guard_evidence_references,
    guard_infeasible,
)
from gui_agent.core.supervisor.statement.policy import StatementSupervisorPolicy
from gui_agent.core.supervisor.statement.schemas import (
    _ActionDraft,
    _StatementTransitionResult,
    _TransitionAction,
    _TransitionEvidence,
)
from gui_agent.core.supervisor.statement.model_io import (
    _evidence_pack_block,
    _semantic_actions_block,
)


def test_guard_complete_only_validates_evidence() -> None:
    ok = guard_complete(
        CompletionEvaluation("satisfied", "done", "confirmed")
    )
    assert ok.allowed and ok.verification == "confirmed"
    bad = guard_complete(
        CompletionEvaluation("pending", "not yet")
    )
    assert not bad.allowed
    assert not hasattr(bad, "fallback_kind")


def test_journal_evidence_reference_must_be_exposed_by_memory() -> None:
    evidence = [
        _TransitionEvidence(source="journal", event_ref="turn:4", claim="commit dispatched")
    ]
    assert guard_evidence_references(evidence, available_refs={"turn:4"}).allowed
    rejected = guard_evidence_references(evidence, available_refs={"turn:3"})
    assert not rejected.allowed
    assert "turn:4" in rejected.reason


def test_current_observation_evidence_needs_no_event_ref() -> None:
    evidence = [
        _TransitionEvidence(source="current_observation", claim="success banner visible")
    ]
    assert guard_evidence_references(evidence, available_refs=set()).allowed


def test_transition_action_requires_one_instruction() -> None:
    assert _TransitionAction(instruction="点击 Save").instruction
    with pytest.raises(ValidationError):
        _TransitionAction()


def test_typed_action_renders_one_canonical_instruction() -> None:
    instruction = StatementSupervisorPolicy._canonical_instruction(
        _ActionDraft(
            instruction="input size and click Search",
            summary="filter",
            atomic_role="write",
            action_family="input",
            target_control="Attribute Code",
            target_value="size",
        )
    )

    assert instruction == "Input 'size' into the visible 'Attribute Code' control."


def test_structured_target_role_prefix_is_normalized_without_prose_parsing() -> None:
    action = _TransitionAction(
        instruction="open the store menu",
        action_family="activate",
        target_control="link: STORES",
    )

    assert action.target_control == "STORES"


def test_complete_evidence_is_enforced_by_runtime_guard() -> None:
    decision = _StatementTransitionResult(
        kind="complete",
        reason="目标已出现",
        evidence=[
            _TransitionEvidence(
                source="current_observation",
                claim="目标页面标题与合同一致",
            )
        ],
    )
    assert decision.kind == "complete"
    missing = _StatementTransitionResult(
        kind="complete",
        reason="looks done",
    )
    assert not guard_evidence_references(
        missing.evidence,
        available_refs=set(),
    ).allowed


def test_complete_discriminator_discards_redundant_action() -> None:
    decision = _StatementTransitionResult.model_validate({
        "kind": "complete",
        "reason": "目标已经满足",
        "action": {
            "instruction": "点击 Catalog",
            "atomic_role": "prepare",
            "action_family": "navigate",
            "target_control": "Catalog",
        },
    })

    assert decision.kind == "complete"
    assert decision.action is None


def test_act_with_complete_action_can_fill_missing_report_reason() -> None:
    decision = _StatementTransitionResult.model_validate({
        "kind": "act",
        "action": {
            "instruction": "点击 Catalog",
            "atomic_role": "prepare",
            "action_family": "navigate",
            "target_control": "Catalog",
        },
    })

    assert decision.reason == "点击 Catalog"


def test_act_drops_unreferenced_journal_explanation() -> None:
    decision = _StatementTransitionResult.model_validate({
        "kind": "act",
        "reason": "需要进入编辑页检查",
        "evidence": [
            {
                "source": "journal",
                "claim": "Previously clicked Save Attribute",
            },
            {
                "source": "current_observation",
                "claim": "Attribute list is visible",
            },
        ],
        "action": {
            "instruction": "打开 size 属性行",
            "action_family": "activate",
        },
    })

    assert decision.kind == "act"
    assert [item.source for item in decision.evidence] == ["current_observation"]


def test_terminal_journal_evidence_still_requires_event_ref() -> None:
    with pytest.raises(ValidationError, match="requires turn:N event_ref"):
        _StatementTransitionResult.model_validate({
            "kind": "complete",
            "reason": "保存已完成",
            "evidence": [
                {
                    "source": "journal",
                    "claim": "Previously clicked Save Attribute",
                }
            ],
        })


def test_guard_uses_runtime_verification_without_model_negotiation() -> None:
    verdict = guard_complete(
        CompletionEvaluation(
            "satisfied",
            "visual semantic evidence only",
            "accepted_unverified",
        ),
    )

    assert verdict.allowed
    assert verdict.verification == "accepted_unverified"


def test_act_requires_one_atomic_action() -> None:
    act = _StatementTransitionResult(
        kind="act",
        reason="需要保存",
        action=_TransitionAction(instruction="点击 Save", atomic_role="commit"),
    )
    assert act.action and act.action.atomic_role == "commit"
    with pytest.raises(ValidationError):
        _StatementTransitionResult(
            kind="act",
            reason="换路线",
        )


def test_infeasible_guard_requires_structure_or_exhausted_recovery() -> None:
    kickback = compose_kickback_directive(
        dead_route="继续寻找缺失入口",
        required_route="使用可验证的新入口",
    )
    denied = guard_infeasible(
        evidence_valid=True,
        structure_complete=False,
        reason="入口未见",
        kickback=kickback,
    )
    assert not denied.allowed
    assert guard_infeasible(
        evidence_valid=True,
        structure_complete=True,
        reason="完整控件清单没有入口",
        kickback=kickback,
    ).allowed


def test_infeasible_transition_requires_kickback_directive() -> None:
    evidence = [
        _TransitionEvidence(source="current_observation", claim="完整清单中无目标控件")
    ]
    missing = _StatementTransitionResult(
        kind="infeasible",
        reason="目标控件不存在",
        evidence=evidence,
    )
    assert not guard_infeasible(
        evidence_valid=True,
        structure_complete=True,
        reason=missing.reason,
        kickback=missing.kickback,
    ).allowed
    malformed = _StatementTransitionResult(
        kind="infeasible",
        reason="目标控件不存在",
        kickback="换一个方法",
        evidence=evidence,
    )
    assert not guard_infeasible(
        evidence_valid=True,
        structure_complete=True,
        reason=malformed.reason,
        kickback=malformed.kickback,
    ).allowed
    decision = _StatementTransitionResult(
        kind="infeasible",
        reason="目标控件不存在",
        kickback=compose_kickback_directive(
            dead_route="继续使用缺失控件",
            required_route="使用可见入口",
        ),
        evidence=evidence,
    )
    assert "【规定路线】" in decision.kickback


def test_evidence_pack_is_context_not_a_route_instruction() -> None:
    block = _evidence_pack_block(
        evaluation_reason="write receipt exists; commit not observed",
        evaluation_status="pending",
        evaluation_verification="in_progress",
        persistence_summary="status=pending terminal_ready=False",
    )

    assert block is not None
    assert "evidence.status：pending" in block.content
    assert "persistence：status=pending" in block.content
    assert "只用于校验 complete" in block.content


def test_semantic_action_inventory_is_positive_evidence_only() -> None:
    block = _semantic_actions_block(
        [
            {"role": "link", "key": "Products"},
            {"role": "heading", "key": "Catalog"},
        ]
    )

    assert block is not None
    assert "link: Products" in block.content
    assert "heading: Catalog" not in block.content
    assert "缺失项本身不能证明" in block.content
