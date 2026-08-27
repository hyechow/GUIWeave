from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from gui_agent.core.tool_agent.contracts import (
    CollectionRef,
    MaterializedFrame,
    WorkerOutcome,
    WorkerSpec,
    WorkerStrategy,
)
from gui_agent.core.tool_agent.runtime import ToolAgentRuntime
from gui_agent.core.tool_agent.strategy import ReflectionResult, Reflector


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


def _reflection(
    replacement: dict,
    *,
    decision: str = "revise_approach",
    reason: str = "The candidate is executable and materially different.",
) -> dict:
    return {
        "diagnosis": {
            "kind": "approach_disproved",
            "evidence_refs": [],
            "reason": reason,
        },
        "recommendation": {
            "decision": decision,
            "approach": (
                replacement["strategy"]["approach"]
                if decision == "revise_approach" else None
            ),
        },
    }


def _revision_runtime(
    replacement: dict,
    *,
    selection: dict | None = None,
    **context,
) -> ToolAgentRuntime:
    runtime = object.__new__(ToolAgentRuntime)
    model = _RecordedStrategyModel(selection or _reflection(replacement))
    runtime.__dict__.update({
        "reflector": Reflector(model),
        "worker": model,
        "_platform_capabilities": frozenset({
            "tap", "type", "select_option", "open_url", "scroll",
        }),
        "trace": [],
        "_worker_last_frames": {},
        "_worker_last_contexts": {},
        "_master_knowledge": "",
        "_worker_knowledge": "",
        "_worker_access_context": "",
        "_status_cb": None,
    }, **context)
    return runtime


def _revise(
    runtime: ToolAgentRuntime,
    worker_id: str,
    original: WorkerSpec,
    summary: str,
) -> WorkerStrategy:
    result = runtime._request_reflection(
        logical_worker_id=worker_id,
        prior_worker_id=worker_id,
        original_spec=original,
        prior_outcome=WorkerOutcome(phase="failed", summary=summary, steps=1),
        failure_reason="Use a different execution path",
        attempt_no=1,
        attempted_approaches=[original.strategy],
    )
    assert result.strategy is not None
    return result.strategy


def test_task214_strategy_replaces_only_the_failed_approach() -> None:
    case = _case()
    original = WorkerSpec.model_validate(case["original_spec"])
    runtime = _revision_runtime(case["replacement_spec"])
    runtime._worker_last_frames = {}
    runtime._master_knowledge = (
        "The same exact product-scoped reviews are reachable through an alternate "
        "visible navigation path."
    )
    runtime._status_cb = None

    result = runtime._request_reflection(
        logical_worker_id=case["logical_worker_id"],
        prior_worker_id=case["logical_worker_id"],
        original_spec=original,
        prior_outcome=WorkerOutcome(
            phase="failed",
            summary="The current review search surface is blocked.",
            failure_kind="worker_blocked",
            steps=3,
        ),
        failure_reason="The current review search surface is blocked.",
        attempt_no=1,
        attempted_approaches=[original.strategy],
    )
    revised, reason = result.strategy, result.reason
    assert revised is not None

    expected = case["expected"]
    assert original.data_requirements[0].filters["product"] == expected["first_query"]
    assert original.data_requirements[0].id == expected["same_downstream_requirement_id"]
    assert revised != original.strategy
    assert revised.model_dump(mode="json") == case["replacement_spec"]["strategy"]
    assert reason
    model = runtime.worker
    assert model.bind_kwargs["response_format"] == {"type": "json_object"}
    assert model.bind_kwargs["max_tokens"] == 600
    prompt = str(model.messages)
    assert "materially different" in prompt
    assert "alternate visible navigation path" in prompt


def test_reflector_consumes_the_exact_last_worker_state_projection() -> None:
    case = _case()
    original = WorkerSpec.model_validate(case["original_spec"])
    runtime = _revision_runtime(case["replacement_spec"])
    projected = {
        "goal_contract": {"goal": "projected goal"},
        "historical_progress": {"latest_transition_ref": "receipt:9"},
        "current_state": {"surface": {"continuity": "returned_to_anchor"}},
    }
    runtime._worker_last_contexts = {"collect_records": projected}

    runtime._request_reflection(
        logical_worker_id="collect_records",
        prior_worker_id="collect_records",
        original_spec=original,
        prior_outcome=WorkerOutcome(
            phase="failed", summary="Current path is disproved", steps=2,
        ),
        failure_reason="Current path is disproved",
        attempt_no=1,
        attempted_approaches=[original.strategy],
    )

    context = json.loads(runtime.worker.messages[1].content)
    assert context["goal_contract"] == projected["goal_contract"]
    assert context["historical_progress"] == projected["historical_progress"]
    assert context["current_state"] == projected["current_state"]


def test_strategy_policy_can_stop_without_dispatching_a_candidate() -> None:
    case = _case()
    original = WorkerSpec.model_validate(case["original_spec"])
    runtime = _revision_runtime(
        case["replacement_spec"],
        selection=_reflection(
            case["replacement_spec"], decision="stop",
            reason="The only candidate repeats the disproven entry path.",
        ),
    )

    result = runtime._request_reflection(
        logical_worker_id="collect_records",
        prior_worker_id="collect_records",
        original_spec=original,
        prior_outcome=WorkerOutcome(
            phase="failed", summary="The entry path is blocked", steps=2
        ),
        failure_reason="The entry path is blocked",
        attempt_no=1,
        attempted_approaches=[original.strategy],
    )

    assert result.strategy is None
    assert "repeats" in result.reason
    selected = [event for event in runtime.trace if event["event"] == "reflection_decision"]
    assert selected[0]["decision"] == "stop"


def test_strategy_rejects_entrypoint_as_an_out_of_boundary_field() -> None:
    case = _case()
    original = WorkerSpec.model_validate(case["original_spec"])
    runtime = _revision_runtime(
        case["replacement_spec"],
        selection={
            "diagnosis": {
                "kind": "approach_disproved", "evidence_refs": [],
                "reason": "Use a different path.",
            },
            "recommendation": {
                "decision": "revise_approach",
                "approach": "open_url https://alternate.example.test/",
            },
        },
    )
    result = runtime._request_reflection(
        logical_worker_id="retrieve_reference",
        prior_worker_id="retrieve_reference",
        original_spec=original,
        prior_outcome=WorkerOutcome(
            phase="failed", summary="Current path blocked", steps=1,
        ),
        failure_reason="Current path blocked",
        attempt_no=1,
        attempted_approaches=[original.strategy],
    )

    assert result.strategy is None
    assert result.reason == "Reflector did not produce a valid recommendation."
    decision = next(
        event for event in runtime.trace if event["event"] == "reflection_decision"
    )
    assert "without an action" in " ".join(decision["diagnostics"])


def test_failed_execution_revision_preserves_the_master_contract() -> None:
    case = _case()
    original = WorkerSpec.model_validate(case["original_spec"])
    runtime = _revision_runtime(case["replacement_spec"])

    revised = _revise(
        runtime, "collect_records", original,
        "The first interaction path was blocked",
    )
    replaced = original.model_copy(update={"strategy": revised})

    assert replaced.model_dump(mode="json", exclude={"strategy"}) == (
        original.model_dump(mode="json", exclude={"strategy"})
    )
    assert revised.model_dump(mode="json") == case["replacement_spec"]["strategy"]


def test_authoritative_empty_result_is_terminal_success_for_every_scope() -> None:
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

    assert Reflector.route(filtered) == "complete"
    assert Reflector.route(unfiltered) == "complete"


def test_task214_authoritative_empty_does_not_dispatch_strategy() -> None:
    case = _case()
    original = WorkerSpec.model_validate(case["original_spec"])
    empty = WorkerOutcome.model_validate(case["empty_outcome"])
    runtime = object.__new__(ToolAgentRuntime)
    runtime.trace = []
    runtime._status_cb = None
    runtime._worker_last_frames = {}
    calls: list[tuple[str, WorkerSpec]] = []

    def run_worker(
        worker_id: str,
        spec: WorkerSpec,
        *,
        require_attempt: bool = False,
    ):
        calls.append((worker_id, spec))
        return empty

    runtime._run_worker = run_worker
    runtime._request_reflection = lambda **_kwargs: (_ for _ in ()).throw(
        AssertionError("completed empty result must not request Strategy")
    )

    outcome = runtime._run_logical_worker(
        case["logical_worker_id"],
        original,
    )

    assert outcome == empty
    assert [worker_id for worker_id, _ in calls] == [case["logical_worker_id"]]
    assert not any(
        event["event"] == "reflected_worker_dispatched" for event in runtime.trace
    )


def test_strategy_stop_does_not_dispatch_another_worker() -> None:
    original = WorkerSpec.model_validate(_case()["original_spec"])
    runtime = object.__new__(ToolAgentRuntime)
    runtime.trace = []
    runtime._status_cb = None
    calls: list[str] = []

    def fail(
        worker_id: str,
        _spec: WorkerSpec,
        *,
        require_attempt: bool = False,
    ) -> WorkerOutcome:
        calls.append(worker_id)
        return WorkerOutcome(phase="failed", summary="Path disproven", steps=2)

    runtime._run_worker = fail
    runtime._request_reflection = lambda **_kwargs: ReflectionResult(
        decision="stop",
        reason="No materially different strategy remains.",
    )

    outcome = runtime._run_logical_worker("collect_records", original)

    assert calls == ["collect_records"]
    assert outcome.steps == 2
    assert "Reflector stopped" in outcome.summary
    assert runtime.trace[-1]["event"] == "reflection_stopped"


class _CompletingWorker:
    """Worker policy that calls `complete` on the first policy turn."""

    def __init__(self) -> None:
        self.calls = 0
        self.mode = ""

    def bind(self, **kwargs):
        del kwargs
        self.mode = "state"
        return self

    def bind_tools(self, tools, **kwargs):
        del kwargs
        names = {tool["function"]["name"] for tool in tools}
        self.mode = "state" if names == {"edit_state_memory"} else "actor"
        return self

    def invoke(self, messages):
        if self.mode == "state":
            payload = json.loads(messages[-1].content[0]["text"])
            return SimpleNamespace(content="", tool_calls=[{
                "id": "state-delta",
                "name": "edit_state_memory",
                "args": {
                    "mode": payload["mode"],
                    "frame_id": payload["frame_id"],
                    "surface": None,
                    "visible_targets": [],
                    "edits": [{
                        "old_lines": [],
                        "new_lines": [
                            "# Observed facts", "",
                            "- The requested collection is visible.",
                        ],
                    }],
                },
            }])
        self.calls += 1
        return SimpleNamespace(
            content="",
            tool_calls=[{
                "id": "complete-1",
                "name": "complete",
                "args": {
                    "evidence": ["Filtered list fits the viewport with no clipped tail."],
                },
            }],
        )


def _collector_runtime(frame: MaterializedFrame, descriptor: CollectionRef,
                       rows: list[dict]) -> ToolAgentRuntime:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.trace = []
    runtime._status_cb = None
    runtime._worker_journals = {}
    runtime._worker_last_frames = {}
    runtime._observe = lambda _spec: (frame, b"png")
    runtime.worker = _CompletingWorker()
    from gui_agent.core.tool_agent.data_store import RuntimeDataStore

    runtime.data_store = RuntimeDataStore()
    runtime.data_store.restore_collection(descriptor, rows)
    return runtime


def test_empty_collection_completes_only_through_a_worker_policy_call() -> None:
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
    runtime = _collector_runtime(frame, empty_collection, [])

    outcome = runtime._run_worker(case["logical_worker_id"], spec)

    # ReAct collection: no runtime auto-completion; the Worker policy is consulted
    # and its explicit `complete` binds the accumulated (empty) collection.
    assert outcome.phase == "completed"
    assert outcome.collection_ref is not None
    assert outcome.collection_ref.row_count == 0
    assert runtime.worker.calls == 1
    assert outcome.steps >= 1
    completed = [event for event in runtime.trace if event["event"] == "worker_complete"]
    assert completed and "completion_source" not in completed[0]


def test_collector_completion_binds_accumulated_collection_without_coverage_gate() -> None:
    case = _case()
    spec = WorkerSpec.model_validate(case["original_spec"])
    rows = [
        {"product": "Erica Sports Bra", "title": "Great fit", "rating": 5},
        {"product": "Erica Sports Bra", "title": "Runs small", "rating": 3},
    ]
    collection = CollectionRef.model_validate(
        case["empty_outcome"]["collection_ref"]
    ).model_copy(update={"row_count": 2})
    frame = MaterializedFrame(
        frame_id="frame:7",
        screenshot_path="recorded-ready.png",
        collections=[collection],
    )
    runtime = _collector_runtime(frame, collection, rows)

    outcome = runtime._run_worker(case["logical_worker_id"], spec)

    assert outcome.phase == "completed"
    assert outcome.collection_ref is not None
    assert outcome.collection_ref.row_count == 2
    assert runtime.worker.calls == 1
    assert outcome.steps >= 1
    completed = [event for event in runtime.trace if event["event"] == "worker_complete"]
    assert completed and "completion_source" not in completed[0]
