from __future__ import annotations

from types import SimpleNamespace

import pytest

from gui_agent.core.orchestrator._validator.governance import (
    TEXTUAL_FALLBACK_HEURISTIC_SAMPLES,
    TEXTUAL_FALLBACK_VALIDATOR_CODES,
)
from gui_agent.core.orchestrator.program import Program, Run
from gui_agent.core.orchestrator.validator import validate_program
from gui_agent.core.schemas import (
    BaseAction,
    BaseActionDecision,
    Milestone,
    PolicyTurn,
    SupervisorStep,
    TargetVerify,
)
from gui_agent.core.supervisor.milestone.schemas import _SingleCheckResult


def _samples(kind: str) -> list[dict[str, object]]:
    return [sample for sample in TEXTUAL_FALLBACK_HEURISTIC_SAMPLES if sample.get("kind") == kind]


def test_textual_fallback_heuristic_registry_has_metadata():
    ids = [str(sample.get("id") or "") for sample in TEXTUAL_FALLBACK_HEURISTIC_SAMPLES]
    assert len(ids) == len(set(ids))
    assert ids
    for sample in TEXTUAL_FALLBACK_HEURISTIC_SAMPLES:
        assert sample.get("id")
        assert sample.get("kind")
        assert sample.get("owner")
        assert sample.get("retire_when")
        assert sample.get("trigger") or sample.get("statements") or sample.get("milestone_name")
        code = sample.get("validator_code")
        if code is not None:
            assert code in TEXTUAL_FALLBACK_VALIDATOR_CODES


@pytest.mark.parametrize("sample", _samples("retrieval_field_extract"), ids=lambda s: str(s["id"]))
def test_retrieval_field_stopword_extract_samples(sample):
    from gui_agent.core.orchestrator._validator.retrieval import _extract_retrieval_fields

    assert _extract_retrieval_fields(str(sample["trigger"])) == sample["expected"]


@pytest.mark.parametrize("sample", _samples("retrieval_same_target"), ids=lambda s: str(s["id"]))
def test_retrieval_same_target_samples(sample):
    from gui_agent.core.orchestrator._validator.retrieval import _mentions_same_retrieval_target

    assert _mentions_same_retrieval_target(str(sample["trigger"])) is sample["expected"]


@pytest.mark.parametrize("sample", _samples("retrieval_field_normalize"), ids=lambda s: str(s["id"]))
def test_retrieval_field_stopword_normalize_samples(sample):
    from gui_agent.core.orchestrator._validator.retrieval import _normalize_retrieval_field

    assert _normalize_retrieval_field(str(sample["trigger"])) == sample["expected"]


@pytest.mark.parametrize("sample", _samples("normalize_confirm_read_gate"), ids=lambda s: str(s["id"]))
def test_terminal_submit_classifier_samples(sample):
    from gui_agent.core.orchestrator.passes import normalize_confirm_read_gates
    from gui_agent.core.supervisor.milestone.helpers import is_dispatch_gate_sc

    statements = [Run(**stmt) for stmt in sample["statements"]]  # type: ignore[arg-type]
    out = normalize_confirm_read_gates(Program(statements=statements))
    terminal = out.statements[-1]
    assert isinstance(terminal, Run)
    assert is_dispatch_gate_sc(terminal.success_condition) is sample["expect_dispatch_gate"]
    if "expect_success_condition" in sample:
        assert terminal.success_condition == sample["expect_success_condition"]
    if "expect_success_condition_contains" in sample:
        assert str(sample["expect_success_condition_contains"]) in terminal.success_condition


def _policy_turn(instruction: str) -> PolicyTurn:
    action = BaseAction(action_type="tap", x=10, y=20, description=instruction)
    return PolicyTurn(
        index=1,
        observation_source="test",
        supervisor=SupervisorStep(
            should_act=True,
            instruction=instruction,
            stop=False,
            goal_completed=False,
            summary="",
            milestone_id="m1",
        ),
        action_decision=BaseActionDecision(action=action),
        target_verify=TargetVerify(on_target=True, actual_element=instruction),
        executed=True,
    )


@pytest.mark.parametrize("sample", _samples("terminal_dispatch_turn"), ids=lambda s: str(s["id"]))
def test_terminal_dispatch_classifier_samples(sample):
    from gui_agent.core.supervisor.milestone import policy as policy_mod

    milestone = Milestone(id="m1", name="提交表单", description="", success_condition="", kind="action")
    assert policy_mod._is_terminal_dispatch_turn(  # noqa: SLF001 - governance test for textual fallback
        _policy_turn(str(sample["trigger"])),
        milestone,
    ) is sample["expected"]


@pytest.mark.parametrize("sample", _samples("negative_action_feedback"), ids=lambda s: str(s["id"]))
def test_negative_action_feedback_classifier_samples(sample):
    from gui_agent.core.supervisor.milestone import policy as policy_mod

    check = _SingleCheckResult(
        status="in_progress",
        reason=str(sample["trigger"]),
        summary=str(sample["trigger"]),
    )
    assert policy_mod._has_negative_action_feedback(check) is sample["expected"]  # noqa: SLF001


@pytest.mark.parametrize("sample", _samples("runtime_preserved_scope_filter"), ids=lambda s: str(s["id"]))
def test_runtime_preserved_scope_classifier_samples(sample):
    from gui_agent.core.supervisor.milestone.helpers import filter_chips_clean

    milestone = Milestone(
        id="m1",
        name=str(sample["milestone_name"]),
        description="",
        success_condition=str(sample["milestone_success_condition"]),
        kind="filter",
    )
    assert filter_chips_clean(sample["applied_filters"], milestone) is sample["expected"]


@pytest.mark.parametrize("sample", _samples("validator_issue"), ids=lambda s: str(s["id"]))
def test_validator_textual_fallback_issue_samples(sample):
    program = Program(statements=[
        Run(
            name=str(sample["trigger"]),
            kind="filter",
            success_condition="Active filters 同时包含客户筛选和 Status=Pending",
        )
    ])
    resolution = SimpleNamespace(entities=[
        SimpleNamespace(mention="Grace Nguyen", search_key="Grace", type="customer"),
    ])

    assert sample["validator_code"] in {
        issue.code for issue in validate_program(program, resolution=resolution)
    }


def test_all_textual_fallback_validator_codes_have_heuristic_samples():
    sampled_codes = {
        str(sample["validator_code"])
        for sample in TEXTUAL_FALLBACK_HEURISTIC_SAMPLES
        if sample.get("validator_code")
    }
    assert TEXTUAL_FALLBACK_VALIDATOR_CODES <= sampled_codes
