from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from gui_agent.core.tool_agent.contracts import (
    CollectionRef,
    DynamicActionSpec,
    MaterializedFrame,
    WorkerOutcome,
    WorkerSpec,
)
from gui_agent.core.tool_agent.runtime import ToolAgentRuntime
from gui_agent.core.tool_agent.strategy import StrategyPlanner


_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "tool_agent"
    / "task214_empty_query_redelegation.json"
)


class _RecordedStrategyModel:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.bind_kwargs: dict = {}
        self.messages = []

    def bind(self, **kwargs):
        self.bind_kwargs = kwargs
        return self

    def invoke(self, messages):
        self.messages = messages
        return SimpleNamespace(
            content=json.dumps(self.response),
            tool_calls=[],
        )


def _case() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _candidate(replacement: dict) -> dict:
    return {
        "hypothesis": "The alternate physical path can satisfy the unchanged subgoal.",
        "invalidated_assumption": "The prior physical path is the only viable path.",
        "strategy": replacement.get("strategy") or "Use the evidenced alternate path.",
        "actions": replacement["actions"],
        "expected_progress": "The target surface visibly advances toward the success criteria.",
        "disconfirming_evidence": "The alternate path visibly fails or repeats the blocker.",
        "evidence_basis": ["bounded execution experience"],
        "estimated_steps": min(int(replacement.get("max_steps") or 12), 20),
        "acquisition_filters": replacement.get("acquisition_filters"),
    }


def _revision_runtime(
    replacement: dict,
    *,
    selection: dict | None = None,
    **context,
) -> ToolAgentRuntime:
    runtime = object.__new__(ToolAgentRuntime)
    proposer = _RecordedStrategyModel({"candidates": [_candidate(replacement)]})
    selector = _RecordedStrategyModel(selection or {
        "decision": "attempt",
        "chosen_index": 0,
        "reason": "The candidate is executable and materially different.",
    })
    runtime.__dict__.update({
        "strategy_planner": StrategyPlanner(proposer, selector=selector),
        "strategy_proposer": proposer,
        "strategy_selector": selector,
        "trace": [],
        "_worker_last_frames": {},
        "_master_knowledge": "",
        "_worker_access_context": "",
        "_status_cb": None,
    }, **context)
    return runtime


def _revise(
    runtime: ToolAgentRuntime,
    worker_id: str,
    original: WorkerSpec,
    summary: str,
) -> WorkerSpec:
    revised, _reason = runtime._select_worker_strategy(
        logical_worker_id=worker_id,
        prior_worker_id=worker_id,
        original_spec=original,
        prior_outcome=WorkerOutcome(phase="failed", summary=summary, steps=1),
        replan_reason="Use a different execution path",
        replan_no=1,
        prior_revisions=[original],
        remaining_steps=20,
    )
    assert revised is not None
    return revised


def test_task214_strategy_planner_selects_a_materially_different_worker_spec() -> None:
    case = _case()
    original = WorkerSpec.model_validate(case["original_spec"])
    empty = WorkerOutcome.model_validate(case["empty_outcome"])
    runtime = _revision_runtime(case["replacement_spec"])
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

    revised, reason = runtime._select_worker_strategy(
        logical_worker_id=case["logical_worker_id"],
        prior_worker_id=case["logical_worker_id"],
        original_spec=original,
        prior_outcome=empty,
        replan_reason=runtime._worker_replan_reason(empty),
        replan_no=1,
        prior_revisions=[],
        remaining_steps=20,
    )
    assert revised is not None

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
    assert reason
    proposer = runtime.strategy_proposer
    selector = runtime.strategy_selector
    assert proposer.bind_kwargs["response_format"] == {"type": "json_object"}
    assert selector.bind_kwargs["max_tokens"] == 500
    prompt = str(proposer.messages)
    assert "immutable logical GUI subgoal" in prompt
    assert "shorter literal" in prompt


def test_strategy_selector_can_stop_without_dispatching_a_candidate() -> None:
    case = _case()
    original = WorkerSpec.model_validate(case["original_spec"])
    runtime = _revision_runtime(
        case["replacement_spec"],
        selection={
            "decision": "stop",
            "chosen_index": None,
            "reason": "The only candidate repeats the disproven entry path.",
        },
    )

    revised, reason = runtime._select_worker_strategy(
        logical_worker_id="collect_records",
        prior_worker_id="collect_records",
        original_spec=original,
        prior_outcome=WorkerOutcome(
            phase="failed", summary="The entry path is blocked", steps=2
        ),
        replan_reason="The entry path is blocked",
        replan_no=1,
        prior_revisions=[original],
        remaining_steps=20,
    )

    assert revised is None
    assert "repeats" in reason
    selected = [event for event in runtime.trace if event["event"] == "strategy_selected"]
    assert selected[0]["decision"] == "stop"


def test_runtime_rejects_semantic_drift_during_local_worker_revision() -> None:
    original = WorkerSpec.model_validate(_case()["original_spec"])
    revised = WorkerSpec.model_validate(_case()["replacement_spec"])
    drifted_requirement = revised.data_requirements[0].model_copy(
        update={
            "description": "Collect any broadly related product reviews.",
            "cardinality": "one",
        }
    )
    drifted = revised.model_copy(
        update={
            "goal": "Collect reviews for any related product.",
            "success_criteria": ["Some related reviews are collected."],
            "data_requirements": [drifted_requirement],
        }
    )

    issues = ToolAgentRuntime._worker_revision_issues(original, drifted)

    assert "goal is immutable across strategy revision" in issues
    assert "success_criteria are immutable across strategy revision" in issues
    assert "data_requirements[0].description is immutable" in issues
    assert "data_requirements[0].cardinality is immutable" in issues


def test_runtime_requires_redelegation_to_replace_explicit_strategy() -> None:
    case = _case()
    original = WorkerSpec.model_validate(case["original_spec"]).model_copy(
        update={"strategy": "Use the current acquisition path"},
    )
    revised = WorkerSpec.model_validate(case["replacement_spec"]).model_copy(
        update={"strategy": original.strategy},
    )

    issues = ToolAgentRuntime._worker_revision_issues(original, revised)

    assert "strategy must materially change across strategy revision" in issues


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


def test_source_free_revision_keeps_selected_public_origin_fixed() -> None:
    case = _case()
    original = WorkerSpec.model_validate(case["original_spec"])
    replacement = case["replacement_spec"]
    replacement["actions"] = [{
        "name": "open_alternate_reference",
        "capability": "open_url",
        "description": "Open the selected alternate reference source",
        "fixed_args": {"url": "https://alternate.example.test/"},
    }]
    runtime = _revision_runtime(
        replacement,
        _task_goal="Retrieve a public reference",
    )
    revised = _revise(
        runtime, "retrieve_reference", original,
        "The current source cannot provide the reference",
    )

    assert revised.actions[0].fixed_args == {
        "url": "https://alternate.example.test/"
    }
    assert revised.actions[0].exposed_args == []


def test_failed_execution_revision_preserves_acquisition_scope() -> None:
    case = _case()
    original = WorkerSpec.model_validate(case["original_spec"])
    replacement = case["replacement_spec"]
    replacement["acquisition_filters"] = {"invented_scope": "invented"}
    runtime = _revision_runtime(replacement)

    revised = _revise(
        runtime, "collect_records", original,
        "The first interaction path was blocked",
    )

    assert revised.acquisition_filters == original.acquisition_filters
    assert revised.actions[0].name == replacement["actions"][0]["name"]


def test_unfiltered_authoritative_empty_result_is_terminal_success() -> None:
    case = _case()
    filtered = WorkerOutcome.model_validate(case["empty_outcome"])
    assert filtered.collection_ref is not None
    unfiltered_collection = filtered.collection_ref.model_copy(update={
        "coverage": {
            **filtered.collection_ref.coverage,
            "requested_filters": {},
            "applied_filters": {},
            "coverage_evidence": "explicit_visual_empty_state",
        }
    })
    unfiltered = filtered.model_copy(update={"collection_ref": unfiltered_collection})

    assert ToolAgentRuntime._is_verified_empty(unfiltered)
    assert ToolAgentRuntime._worker_replan_reason(unfiltered) == ""


def test_task214_empty_result_dispatches_a_new_physical_worker(tmp_path: Path) -> None:
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
    runtime._worker_last_frames = {}
    screenshot = tmp_path / "frame.png"
    screenshot.write_bytes(b"png")
    calls: list[tuple[str, WorkerSpec]] = []

    def run_worker(worker_id: str, spec: WorkerSpec):
        calls.append((worker_id, spec))
        if len(calls) == 1:
            runtime._worker_last_frames[worker_id] = MaterializedFrame(
                frame_id="frame:empty",
                screenshot_path=str(screenshot),
            )
        return empty if len(calls) == 1 else recovered

    runtime._run_worker = run_worker
    runtime._select_worker_strategy = lambda **_kwargs: (revised, "selected")

    outcome = runtime._run_worker_with_local_replanning(
        case["logical_worker_id"],
        original,
    )

    assert outcome == recovered.model_copy(
        update={"steps": empty.steps + recovered.steps}
    )
    assert [worker_id for worker_id, _ in calls] == [
        case["logical_worker_id"],
        case["expected"]["physical_worker_id"],
    ]
    assert calls[0][1].acquisition_filters == {"product": "Erica Sports Bra"}
    assert calls[1][1].acquisition_filters == {"product": "Erica"}
    assert any(event["event"] == "strategy_attempt_dispatched" for event in runtime.trace)


def test_strategy_stop_does_not_dispatch_another_worker() -> None:
    original = WorkerSpec.model_validate(_case()["original_spec"])
    runtime = object.__new__(ToolAgentRuntime)
    runtime.max_subgoal_replans = 1
    runtime.trace = []
    runtime._status_cb = None
    calls: list[str] = []

    def fail(worker_id: str, _spec: WorkerSpec) -> WorkerOutcome:
        calls.append(worker_id)
        return WorkerOutcome(phase="failed", summary="Path disproven", steps=2)

    runtime._run_worker = fail
    runtime._select_worker_strategy = lambda **_kwargs: (
        None,
        "No materially different strategy remains.",
    )

    outcome = runtime._run_worker_with_local_replanning("collect_records", original)

    assert calls == ["collect_records"]
    assert outcome.steps == 2
    assert "Strategy Planner stopped" in outcome.summary
    assert runtime.trace[-1]["event"] == "strategy_stopped"


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
    runtime._select_worker_strategy = lambda **_kwargs: (revised, "selected")

    outcome = runtime._run_worker_with_local_replanning("apply_target", original)

    assert calls == [
        ("apply_target", 15),
        ("apply_target_replan_1", 15),
        ("apply_target_replan_2", 10),
    ]
    assert outcome.phase == "failed"
    assert outcome.steps == 40
    assert outcome.summary == "No progress"


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
