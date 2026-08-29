import pytest

from gui_agent.core.tool_agent.contracts import (
    WorkerSpec,
    WorkerStateUpdate,
    WorkerStrategy,
)
from gui_agent.core.tool_agent.state_trace import (
    latest_runtime_receipt,
    latest_runtime_receipts,
    reduce_worker_state,
    state_actor_markdown,
    state_continuation_payload,
    state_observation_focus,
)
from gui_agent.core.tool_agent.worker_memory import WorkerJournal


def _spec() -> WorkerSpec:
    goal = "Set every target property to the requested value."
    return WorkerSpec(
        profile="operator",
        goal=goal,
        success_criteria=[goal],
        strategy=WorkerStrategy(approach="Observe facts, then act."),
    )


def _update(**values) -> WorkerStateUpdate:
    return WorkerStateUpdate.model_validate({
        "status": "advance",
        "objective": "The visible targets have the requested value.",
        **values,
    })


def test_state_normalizes_compact_flash_empty_values() -> None:
    update = WorkerStateUpdate.model_validate({
        "status": "advance",
        "objective": "The requested surface is visible.",
        "targets": [],
        "memory": None,
        "evidence": [],
    })

    assert update.memory == {}
    assert update.evidence == []
    assert update.rows == []


def test_state_normalizes_memory_keys_from_lightweight_models() -> None:
    update = _update(memory={
        "visibleRecordPrimary": True,
        "spaced fact": "value",
    })

    assert update.memory == {
        "visible_record_primary": True,
        "spaced_fact": "value",
    }


def test_state_normalizes_structured_target_identity_from_lightweight_models() -> None:
    update = _update(targets=[{
        "identity": "Visible account row",
        "current_value": "Following",
        "desired_value": "Follow",
    }])

    assert update.targets == ["Visible account row"]


def test_state_serializes_facts_before_transition() -> None:
    update = _update(memory={"observed_value": "ready"})

    assert list(update.model_dump())[:4] == [
        "memory", "status", "objective", "targets",
    ]


def test_state_merges_fact_patch_and_derives_stable_target_refs() -> None:
    first = reduce_worker_state(
        None,
        _update(
            targets=["Stable visible record"],
            memory={"requested_value": False, "obsolete_fact": "old"},
        ),
        frame_id="frame:1",
    )
    (target_ref,) = first.targets
    second = reduce_worker_state(
        first,
        _update(
            targets=["Stable visible record"],
            memory={"requested_value": True, "obsolete_fact": None},
        ),
        frame_id="frame:2",
    )

    assert list(second.targets) == [target_ref]
    assert second.targets[target_ref].identity == "Stable visible record"
    assert second.memory == {"requested_value": True}
    assert second.task_transition is not None
    assert second.task_transition.target_refs == [target_ref]


def test_state_rejects_incoherent_transitions_and_duplicate_targets() -> None:
    with pytest.raises(ValueError, match="non-empty objective"):
        _update(objective="")
    with pytest.raises(ValueError, match="evidence or rows"):
        _update(status="complete", objective="", targets=[])
    with pytest.raises(ValueError, match="duplicate identities"):
        _update(targets=["Same record", "Same record"])


def test_state_can_patch_decisive_fact_and_complete_atomically() -> None:
    initial = reduce_worker_state(
        None,
        _update(memory={"requested_value": False}),
        frame_id="frame:1",
    )
    complete = WorkerStateUpdate.model_validate({
        "status": "complete",
        "objective": "",
        "targets": [],
        "memory": {"requested_value": True},
        "evidence": "The requested value is visibly true.",
    })
    final = reduce_worker_state(
        initial,
        complete,
        frame_id="frame:2",
    )

    assert final.memory == {"requested_value": True}
    assert final.task_transition is not None
    assert final.task_transition.status == "complete"
    assert complete.evidence == ["The requested value is visibly true."]


def test_state_actor_projection_contains_only_current_bindings_and_compact_memory() -> None:
    state = reduce_worker_state(
        None,
        _update(
            targets=["Message from Echo"],
            memory={"received_date": "2025-10-20"},
        ),
        frame_id="frame:1",
    )

    payload = state_actor_markdown(state)

    (target_ref,) = state.targets
    assert f"`{target_ref}` — Message from Echo" in payload
    assert '"received_date": "2025-10-20"' in payload
    assert "old_lines" not in payload
    assert "visible_targets" not in payload


def test_state_continuation_uses_identities_not_runtime_refs() -> None:
    state = reduce_worker_state(
        None,
        _update(
            targets=["Stable record"],
            memory={"visible_value": 3},
        ),
        frame_id="frame:1",
    )

    continuation = state_continuation_payload(state)

    assert continuation == {
        "memory": {"visible_value": 3},
        "previous_transition": {
            "status": "advance",
            "objective": "The visible targets have the requested value.",
            "targets": ["Stable record"],
        },
    }
    assert "target_" not in str(continuation)


def test_state_bounds_continuous_memory() -> None:
    with pytest.raises(ValueError, match="exceeds 16000"):
        reduce_worker_state(
            None,
            _update(memory={"oversized_fact": "x" * 16_001}),
            frame_id="frame:1",
        )


def test_state_observation_focus_withholds_filters_but_exposes_goal_contract() -> None:
    spec = WorkerSpec.model_validate({
        **_spec().model_dump(mode="json"),
        "profile": "collector",
        "data_requirements": [{
            "id": "records",
            "description": "Records on or after the private boundary",
            "row_schema": {
                "type": "object",
                "properties": {"received_date": {"type": "string"}},
            },
            "field_sources": {"received_date": "visible received date"},
            "filters": {"received_date": {"from": "2025-10-03"}},
        }],
        "completion_facts": [{
            "property_ref": "commit_observed",
            "description": "The requested commit is visibly established.",
            "expected_value": True,
        }],
    })

    focus = state_observation_focus(spec)

    assert focus["visible_fields"] == ["received_date", "visible received date"]
    assert focus["goal_contract"]["success_criteria"] == [
        "Set every target property to the requested value.",
    ]
    assert focus["goal_contract"]["completion_facts"] == [{
        "property_ref": "commit_observed",
        "description": "The requested commit is visibly established.",
        "expected_value": True,
    }]
    assert "2025-10-03" not in str(focus)


def test_state_receipt_is_small_observation_context() -> None:
    journal = WorkerJournal(worker_id="worker")
    journal.record_action_result(
        step=1,
        frame_id="frame:1",
        tool="scroll",
        args={
            "direction": "down",
            "description": "Reveal more matching records",
            "private_value": "do-not-project",
        },
        result={"status": "executed", "action_type": "scroll", "no_effect": True},
        commitment_refs=(),
    )

    receipt = latest_runtime_receipt(journal)

    assert receipt is not None
    assert receipt["traversal_direction"] == "down"
    assert receipt["action_description"] == "Reveal more matching records"
    assert "private_value" not in str(receipt)


def test_state_projects_the_whole_latest_multi_action_receipt_batch() -> None:
    journal = WorkerJournal(worker_id="worker")
    for substep, target in enumerate(("First row", "Second row"), start=1):
        journal.record_action_result(
            step=2,
            substep=substep,
            frame_id="frame:2",
            tool="tap",
            args={"description": f"Change {target}"},
            result={"status": "executed", "action_type": "tap"},
            state_target_ref=f"target_{substep}",
        )

    receipts = latest_runtime_receipts(journal)

    assert [item["receipt_ref"] for item in receipts] == ["step:2.1", "step:2.2"]
    assert [item["state_target_ref"] for item in receipts] == ["target_1", "target_2"]
