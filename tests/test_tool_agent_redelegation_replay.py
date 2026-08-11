from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from gui_agent.core.tool_agent.contracts import (
    CollectionRef,
    MaterializedFrame,
    WorkerOutcome,
    WorkerSpec,
)
from gui_agent.core.tool_agent.runtime import ToolAgentRuntime


_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "tool_agent"
    / "task214_empty_query_redelegation.json"
)


class _RecordedRevisionMaster:
    def __init__(self, replacement: dict) -> None:
        self.replacement = replacement
        self.bind_kwargs: dict = {}
        self.messages = []

    def bind(self, **kwargs):
        self.bind_kwargs = kwargs
        return self

    def invoke(self, messages):
        self.messages = messages
        return SimpleNamespace(
            content=json.dumps({"worker_spec": self.replacement}),
            tool_calls=[],
        )


def _case() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def test_task214_master_replays_a_materially_different_worker_spec() -> None:
    case = _case()
    original = WorkerSpec.model_validate(case["original_spec"])
    empty = WorkerOutcome.model_validate(case["empty_outcome"])
    master = _RecordedRevisionMaster(case["replacement_spec"])
    runtime = object.__new__(ToolAgentRuntime)
    runtime.master = master
    runtime.trace = [
        {
            "worker_id": case["logical_worker_id"],
            **event,
        }
        for event in case["execution_experience"]
    ]
    runtime._worker_last_frames = {}
    runtime._master_knowledge = (
        "When the full product mention returns an empty review query, use the "
        "shorter literal only for the replacement acquisition attempt."
    )
    runtime._status_cb = None

    revised = runtime._revise_worker_spec(
        logical_worker_id=case["logical_worker_id"],
        prior_worker_id=case["logical_worker_id"],
        original_spec=original,
        prior_outcome=empty,
        replan_reason=runtime._worker_replan_reason(empty),
        replan_no=1,
        prior_revisions=[],
    )

    expected = case["expected"]
    assert original.data_requirements[0].filters["product"] == expected["first_query"]
    assert revised.data_requirements[0].filters == original.data_requirements[0].filters
    assert original.acquisition_filters == {"product": expected["first_query"]}
    assert revised.acquisition_filters == {"product": expected["replacement_query"]}
    assert revised.data_requirements[0].id == expected["same_downstream_requirement_id"]
    assert revised.goal == original.goal
    assert revised.success_criteria == original.success_criteria
    assert revised.data_requirements[0].description == original.data_requirements[0].description
    assert revised.data_requirements[0].row_schema == original.data_requirements[0].row_schema
    assert revised != original
    assert master.bind_kwargs["response_format"] == {"type": "json_object"}
    prompt = str(master.messages)
    assert "same logical subgoal" in prompt
    assert "shorter literal" in prompt


def test_runtime_rejects_semantic_drift_during_local_worker_revision() -> None:
    original = WorkerSpec.model_validate(_case()["original_spec"])
    revised = WorkerSpec.model_validate(_case()["replacement_spec"])
    drifted_requirement = revised.data_requirements[0].model_copy(
        update={"description": "Collect any broadly related product reviews."}
    )
    drifted = revised.model_copy(
        update={
            "goal": "Collect reviews for any related product.",
            "success_criteria": ["Some related reviews are collected."],
            "data_requirements": [drifted_requirement],
        }
    )

    issues = ToolAgentRuntime._worker_revision_issues(original, drifted)

    assert "goal is immutable across runtime redelegation" in issues
    assert "success_criteria are immutable across runtime redelegation" in issues
    assert "data_requirements[0].description is immutable" in issues


def test_authoritative_empty_result_requires_a_new_acquisition_scope() -> None:
    case = _case()
    original = WorkerSpec.model_validate(case["original_spec"])
    empty = WorkerOutcome.model_validate(case["empty_outcome"])
    revised = WorkerSpec.model_validate(case["replacement_spec"]).model_copy(
        update={"acquisition_filters": original.acquisition_filters}
    )

    issues = ToolAgentRuntime._worker_revision_issues(
        original,
        revised,
        prior_outcome=empty,
    )

    assert (
        "acquisition_filters must change after an authoritative empty result"
        in issues
    )


def test_task214_empty_result_dispatches_a_new_physical_worker() -> None:
    case = _case()
    original = WorkerSpec.model_validate(case["original_spec"])
    revised = WorkerSpec.model_validate(case["replacement_spec"])
    empty = WorkerOutcome.model_validate(case["empty_outcome"])
    recovered_collection = CollectionRef(
        ref="collection:erica_review_details",
        requirement_id="erica_review_details",
        chunk_refs=["chunk:erica_review_details:2"],
        row_count=2,
        row_schema=revised.data_requirements[0].row_schema,
        coverage={"scope_status": "met", "status": "complete"},
    )
    recovered = WorkerOutcome(
        phase="completed",
        summary="Replacement exact query collected two review details.",
        collection_ref=recovered_collection,
        steps=4,
    )
    runtime = object.__new__(ToolAgentRuntime)
    runtime.max_subgoal_replans = 1
    runtime.trace = []
    runtime._status_cb = None
    calls: list[tuple[str, WorkerSpec]] = []

    def run_worker(worker_id: str, spec: WorkerSpec) -> WorkerOutcome:
        calls.append((worker_id, spec))
        return empty if len(calls) == 1 else recovered

    runtime._run_worker = run_worker
    runtime._revise_worker_spec = lambda **_kwargs: revised

    outcome = runtime._run_worker_with_local_replanning(
        case["logical_worker_id"],
        original,
    )

    assert outcome == recovered
    assert [worker_id for worker_id, _ in calls] == [
        case["logical_worker_id"],
        case["expected"]["physical_worker_id"],
    ]
    assert calls[0][1].acquisition_filters == {"product": "Erica Sports Bra"}
    assert calls[1][1].acquisition_filters == {"product": "Erica"}
    assert any(event["event"] == "master_worker_redelegated" for event in runtime.trace)


def test_failed_operator_replans_only_its_local_execution_strategy() -> None:
    original = WorkerSpec.model_validate({
        "profile": "operator",
        "goal": "Open account settings.",
        "success_criteria": ["The account settings surface is visible."],
        "actions": [{
            "name": "open_settings_directly",
            "capability": "tap",
            "description": "Open the visible settings entry directly.",
        }],
        "max_steps": 8,
    })
    revised = WorkerSpec.model_validate({
        "profile": "operator",
        "goal": original.goal,
        "success_criteria": original.success_criteria,
        "actions": [{
            "name": "open_account_menu",
            "capability": "tap",
            "description": "Open the account menu before navigating to settings.",
        }],
        "max_steps": 10,
    })
    failed = WorkerOutcome(
        phase="failed",
        summary="The direct settings entry was not available in the current layout.",
        steps=4,
    )
    completed = WorkerOutcome(
        phase="completed",
        summary="Account settings is visible.",
        steps=3,
    )
    runtime = object.__new__(ToolAgentRuntime)
    runtime.max_subgoal_replans = 1
    runtime.trace = []
    runtime._status_cb = None
    calls: list[tuple[str, WorkerSpec]] = []

    def run_worker(worker_id: str, spec: WorkerSpec) -> WorkerOutcome:
        calls.append((worker_id, spec))
        return failed if len(calls) == 1 else completed

    runtime._run_worker = run_worker
    runtime._revise_worker_spec = lambda **_kwargs: revised

    outcome = runtime._run_worker_with_local_replanning(
        "open_account_settings",
        original,
    )

    assert outcome == completed
    assert [worker_id for worker_id, _ in calls] == [
        "open_account_settings",
        "open_account_settings_replan_1",
    ]
    assert calls[1][1].goal == original.goal
    assert calls[1][1].success_criteria == original.success_criteria
    requested = next(
        event for event in runtime.trace
        if event["event"] == "master_worker_replan_requested"
    )
    assert "direct settings entry" in requested["reason"]


def test_redelegation_shares_one_bounded_logical_step_budget() -> None:
    original = WorkerSpec.model_validate({
        "profile": "operator",
        "goal": "Apply a target UI state.",
        "success_criteria": ["The target UI state is saved."],
        "actions": [{
            "name": "apply_target",
            "capability": "tap",
            "description": "Apply the visible target state.",
        }],
        "max_steps": 15,
    })
    revised = original.model_copy(update={
        "actions": [
            original.actions[0].model_copy(update={
                "name": "apply_target_alternatively",
            })
        ],
        "max_steps": 15,
    })
    runtime = object.__new__(ToolAgentRuntime)
    runtime.max_subgoal_replans = 2
    runtime.trace = []
    runtime._status_cb = None
    calls: list[tuple[str, int]] = []

    def run_worker(worker_id: str, spec: WorkerSpec) -> WorkerOutcome:
        calls.append((worker_id, spec.max_steps))
        return WorkerOutcome(
            phase="failed",
            summary="No progress",
            steps=spec.max_steps,
        )

    runtime._run_worker = run_worker
    runtime._revise_worker_spec = lambda **_kwargs: revised

    outcome = runtime._run_worker_with_local_replanning("apply_target", original)

    assert calls == [
        ("apply_target", 15),
        ("apply_target_replan_1", 15),
        ("apply_target_replan_2", 10),
    ]
    assert outcome.phase == "failed"
    assert outcome.steps == 40
    assert "local strategy budget" in outcome.summary


def test_worker_returns_verified_empty_without_another_policy_call() -> None:
    case = _case()
    spec = WorkerSpec.model_validate(case["original_spec"])
    empty_collection = CollectionRef.model_validate(
        case["empty_outcome"]["collection_ref"]
    )
    frame = MaterializedFrame(
        frame_id="frame:6",
        screenshot_path="recorded-task214-turn6.png",
        applied_filters={"Product": "Erica Sports Bra"},
        requirement_scopes={
            "erica_review_details": {
                "status": "met",
                "requested_filters": {"Product": "Erica Sports Bra"},
                "applied_filters": {"Product": "Erica Sports Bra"},
            }
        },
        collections=[empty_collection],
    )
    runtime = object.__new__(ToolAgentRuntime)
    runtime.trace = []
    runtime._status_cb = None
    runtime._worker_journals = {}
    runtime._worker_last_frames = {}
    runtime._observe = lambda _spec: (frame, b"png")
    runtime.worker = SimpleNamespace(
        bind_tools=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("verified empty collection must terminate before policy")
        )
    )

    outcome = runtime._run_worker(case["logical_worker_id"], spec)

    assert outcome.phase == "completed"
    assert outcome.collection_ref is not None
    assert outcome.collection_ref.row_count == 0
    assert outcome.steps == 0
    assert any(event["event"] == "worker_empty_collection" for event in runtime.trace)
