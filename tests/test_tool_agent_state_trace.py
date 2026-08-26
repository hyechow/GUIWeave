from gui_agent.core.tool_agent.contracts import (
    WorkerSpec,
    WorkerStateTraceBatch,
    WorkerStrategy,
)
from gui_agent.core.tool_agent.state_trace import (
    latest_runtime_receipt,
    normalize_state_trace_payload,
    reduce_worker_state,
    state_actor_payload,
    state_continuation_payload,
)
from gui_agent.core.tool_agent.worker_memory import WorkerJournal


def _spec() -> WorkerSpec:
    criterion = "Every target in the collection is resolved."
    return WorkerSpec(
        profile="operator",
        goal=criterion,
        success_criteria=[criterion],
        strategy=WorkerStrategy(approach="Goal-state reconciliation."),
    )


def _batch(mode: str, frame_id: str, events: list[dict]) -> WorkerStateTraceBatch:
    return WorkerStateTraceBatch.model_validate({
        "mode": mode,
        "frame_id": frame_id,
        "events": events,
    })


def test_state_receipt_is_small_observation_context() -> None:
    journal = WorkerJournal(worker_id="worker")
    journal.record_action_result(
        step=1,
        frame_id="frame:1",
        tool="scroll",
        args={"direction": "down", "private_value": "do-not-project"},
        result={"status": "executed", "action_type": "scroll", "no_effect": True},
        commitment_refs=(),
    )

    receipt = latest_runtime_receipt(journal)

    assert receipt is not None
    assert receipt["traversal_direction"] == "down"
    assert "private_value" not in str(receipt)


def test_state_trace_expands_grouped_delta_before_validation() -> None:
    payload = normalize_state_trace_payload({
        "mode": "append",
        "frame_id": "frame:2",
        "delta": {
            "source": ["target_collection", "Collection visible."],
            "surface": ["results", "Results visible."],
            "targets": [[
                "stable_target", "Stable identity", "full", "unobscured", "Target visible.",
            ]],
            "properties": [[
                "stable_target", "requested_state", True, "resolved", "explicit_visual",
                "Filled control.",
            ]],
            "coverage": [["target_collection", "unresolved", "More rows."]],
            "conditions": [["criterion_1", "unresolved", "More rows."]],
        },
    })

    events = WorkerStateTraceBatch.model_validate(payload).events

    assert [event.kind for event in events] == [
        "source_observed", "surface_observed", "target_observed",
        "property_observed", "coverage_observed", "goal_condition_observed",
    ]
    assert events[3].target_ref == "stable_target"
    assert events[3].value is True
    assert events[3].goal_relation == "resolved"


def test_state_trace_appends_to_one_stable_target_ref() -> None:
    initial = reduce_worker_state(None, _batch("init", "frame:1", [
        {
            "kind": "source_observed",
            "source_ref": "target_collection",
            "evidence": "The collection is visible.",
        },
        {
            "kind": "target_observed",
            "target_ref": "stable_target",
            "identity": "@author; clipped content",
            "visibility": "partial",
            "owned_region_visibility": "edge_fragment",
            "evidence": "The row is clipped.",
        },
    ]), spec=_spec())
    appended = reduce_worker_state(initial, _batch("append", "frame:2", [
        {
            "kind": "target_observed",
            "target_ref": "stable_target",
            "identity": "@author; complete stable content",
            "visibility": "full",
            "owned_region_visibility": "unobscured",
            "evidence": "The same row is now complete.",
        },
        {
            "kind": "property_observed",
            "target_ref": "stable_target",
            "property_ref": "requested_state",
            "value": True,
            "goal_relation": "resolved",
            "authority": "explicit_visual",
            "evidence": "The target control confirms the requested state.",
        },
    ]), spec=_spec())

    assert list(appended.targets) == ["stable_target"]
    assert appended.targets["stable_target"].identity.endswith("complete stable content")
    assert state_continuation_payload(appended)["targets"]["stable_target"][
        "relation"
    ] == "resolved"


def test_state_trace_never_guesses_identity_aliases() -> None:
    initial = reduce_worker_state(None, _batch("init", "frame:1", [
        {
            "kind": "source_observed",
            "source_ref": "target_collection",
            "evidence": "The collection is visible.",
        },
        {
            "kind": "target_observed",
            "target_ref": "first_ref",
            "identity": "Same visible identity",
            "visibility": "full",
            "owned_region_visibility": "unobscured",
            "evidence": "The row is visible.",
        },
    ]), spec=_spec())
    appended = reduce_worker_state(initial, _batch("append", "frame:2", [{
        "kind": "target_observed",
        "target_ref": "second_ref",
        "identity": "Same visible identity",
        "visibility": "full",
        "owned_region_visibility": "unobscured",
        "evidence": "The model emitted a different ref.",
    }]), spec=_spec())

    assert set(appended.targets) == {"first_ref", "second_ref"}


def test_resolved_property_is_continuous_memory() -> None:
    spec = _spec()
    state = reduce_worker_state(None, _batch("init", "frame:1", [
        {
            "kind": "source_observed",
            "source_ref": "target_collection",
            "evidence": "The collection is visible.",
        },
        {
            "kind": "target_observed",
            "target_ref": "stable_target",
            "identity": "Stable target",
            "visibility": "full",
            "owned_region_visibility": "unobscured",
            "evidence": "The target is visible.",
        },
        {
            "kind": "property_observed",
            "target_ref": "stable_target",
            "property_ref": "requested_state",
            "value": True,
            "goal_relation": "resolved",
            "authority": "explicit_visual",
            "evidence": "The target detail confirms the requested state.",
        },
    ]), spec=spec)
    state = reduce_worker_state(state, _batch("append", "frame:2", [
        {
            "kind": "target_observed",
            "target_ref": "stable_target",
            "identity": "Stable target",
            "visibility": "full",
            "owned_region_visibility": "unobscured",
            "evidence": "The row is visible again.",
        },
        {
            "kind": "property_observed",
            "target_ref": "stable_target",
            "property_ref": "requested_state",
            "value": False,
            "goal_relation": "unresolved",
            "authority": "bound_visual",
            "evidence": "Weaker row evidence appears inactive.",
        },
    ]), spec=spec)

    prop = state.targets["stable_target"].properties["requested_state"]
    assert prop.value is True
    assert prop.goal_relation == "resolved"


def test_reducer_ignores_repeated_fact_values() -> None:
    spec = _spec()
    state = reduce_worker_state(None, _batch("init", "frame:1", [
        {
            "kind": "source_observed",
            "source_ref": "target_collection",
            "evidence": "The target collection is visible.",
        },
        {
            "kind": "target_observed",
            "target_ref": "stable_target",
            "identity": "Stable target",
            "visibility": "full",
            "owned_region_visibility": "unobscured",
            "evidence": "The target is visible.",
        },
        {
            "kind": "property_observed",
            "target_ref": "stable_target",
            "property_ref": "requested_state",
            "value": False,
            "goal_relation": "unresolved",
            "authority": "explicit_visual",
            "evidence": "Original property evidence.",
        },
        {
            "kind": "coverage_observed",
            "source_ref": "target_collection",
            "status": "unresolved",
            "evidence": "Original coverage evidence.",
        },
    ]), spec=spec)
    state = reduce_worker_state(state, _batch("append", "frame:2", [
        {
            "kind": "target_observed",
            "target_ref": "stable_target",
            "identity": "Stable target",
            "visibility": "full",
            "owned_region_visibility": "unobscured",
            "evidence": "The target remains visible.",
        },
        {
            "kind": "property_observed",
            "target_ref": "stable_target",
            "property_ref": "requested_state",
            "value": False,
            "goal_relation": "unresolved",
            "authority": "explicit_visual",
            "evidence": "Repeated property evidence.",
        },
        {
            "kind": "coverage_observed",
            "source_ref": "target_collection",
            "status": "unresolved",
            "evidence": "Repeated coverage evidence.",
        },
    ]), spec=spec)

    prop = state.targets["stable_target"].properties["requested_state"]
    assert (prop.frame_id, prop.evidence) == (
        "frame:1", "Original property evidence.",
    )
    coverage = state.coverage["target_collection"]
    assert (coverage.frame_id, coverage.evidence) == (
        "frame:1", "Original coverage evidence.",
    )


def test_actor_projection_contains_only_current_frontier_and_resolved_exclusion() -> None:
    state = reduce_worker_state(None, _batch("init", "frame:1", [
        {
            "kind": "source_observed",
            "source_ref": "target_collection",
            "evidence": "The collection is visible.",
        },
        *[
            {
                "kind": "target_observed",
                "target_ref": ref,
                "identity": ref,
                "visibility": "full",
                "owned_region_visibility": "unobscured",
                "evidence": f"{ref} is visible.",
            }
            for ref in ("resolved_target", "open_target")
        ],
        {
            "kind": "property_observed",
            "target_ref": "resolved_target",
            "property_ref": "requested_state",
            "value": True,
            "goal_relation": "resolved",
            "authority": "explicit_visual",
            "evidence": "The state is resolved.",
        },
        {
            "kind": "property_observed",
            "target_ref": "open_target",
            "property_ref": "requested_state",
            "value": False,
            "goal_relation": "unresolved",
            "authority": "explicit_visual",
            "evidence": "The state is unresolved.",
        },
    ]), spec=_spec())

    payload = state_actor_payload(state)

    assert payload["resolved_target_refs"] == ["resolved_target"]
    assert payload["visible_targets"]["resolved_refs_do_not_repeat"] == [
        "resolved_target"
    ]
    assert [item["target_ref"] for item in payload["visible_targets"][
        "unresolved_frontier"
    ]] == ["open_target"]
    assert "identity" not in payload["visible_targets"]["unresolved_frontier"][0]
    assert "summary" not in payload


def test_goal_condition_event_is_the_only_terminal_signal() -> None:
    spec = _spec()
    state = reduce_worker_state(None, _batch("init", "frame:1", [{
        "kind": "goal_condition_observed",
        "condition_ref": "criterion_1",
        "status": "satisfied",
        "evidence": "Continuous goal evidence is complete.",
    }]), spec=spec)

    assert state.status == "completed"
