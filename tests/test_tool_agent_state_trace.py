import pytest

from gui_agent.core.tool_agent.contracts import (
    WorkerSpec,
    WorkerStateEditBatch,
    WorkerStrategy,
)
from gui_agent.core.tool_agent.state_trace import (
    latest_runtime_receipt,
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


def _batch(**values) -> WorkerStateEditBatch:
    return WorkerStateEditBatch.model_validate(values)


def test_state_initializes_open_markdown_with_current_bindings() -> None:
    state = reduce_worker_state(None, _batch(
        mode="init",
        frame_id="frame:1",
        surface="Record list",
        visible_targets=[{
            "target_ref": "stable_record",
            "identity": "Stable visible record",
            "visibility": "full",
            "owned_region_visibility": "unobscured",
        }],
        edits=[{
            "old_lines": [],
            "new_lines": ["### stable_record", "- Visible value: 3"],
        }],
    ), spec=_spec())

    assert state.markdown == "### stable_record\n- Visible value: 3"
    assert state.targets["stable_record"].visibility == "full"
    assert "memory chars=" in state.summary


def test_state_edits_one_document_and_reuses_target_ref() -> None:
    initial = reduce_worker_state(None, _batch(
        mode="init",
        frame_id="frame:1",
        visible_targets=[{
            "target_ref": "stable_record",
            "identity": "Record in list",
            "visibility": "partial",
            "owned_region_visibility": "edge_fragment",
        }],
        edits=[{
            "old_lines": [],
            "new_lines": ["### stable_record", "- Attachment: form.pdf"],
        }],
    ), spec=_spec())
    edited = reduce_worker_state(initial, _batch(
        mode="edit",
        frame_id="frame:2",
        visible_targets=[{
            "target_ref": "stable_record",
            "identity": "Record detail",
            "visibility": "full",
            "owned_region_visibility": "unobscured",
        }],
        edits=[{
            "old_lines": ["- Attachment: form.pdf"],
            "new_lines": ["- Attachment: form.pdf", "  - Downloaded"],
        }],
    ), spec=_spec())

    assert list(edited.targets) == ["stable_record"]
    assert edited.markdown.endswith("  - Downloaded")
    continuation = state_continuation_payload(edited)
    assert continuation["target_registry"] == {"stable_record": "Record detail"}
    assert continuation["memory_markdown"] == edited.markdown


def test_state_edit_requires_one_exact_old_lines_match() -> None:
    initial = reduce_worker_state(None, _batch(
        mode="init", frame_id="frame:1",
        edits=[{"old_lines": [], "new_lines": ["### record", "- Value: one"]}],
    ), spec=_spec())

    with pytest.raises(ValueError, match="exactly once"):
        reduce_worker_state(initial, _batch(
            mode="edit", frame_id="frame:2",
            edits=[{"old_lines": ["missing"], "new_lines": ["replacement"]}],
        ), spec=_spec())
    with pytest.raises(ValueError, match="empty old_lines"):
        reduce_worker_state(initial, _batch(
            mode="edit", frame_id="frame:2",
            edits=[{"old_lines": [], "new_lines": ["append"]}],
        ), spec=_spec())


def test_actor_projection_is_markdown_plus_current_binding_envelope() -> None:
    state = reduce_worker_state(None, _batch(
        mode="init",
        frame_id="frame:1",
        surface="Inbox",
        visible_targets=[{
            "target_ref": "message_echo",
            "identity": "Message from Echo",
            "visibility": "full",
            "owned_region_visibility": "unobscured",
        }],
        edits=[{
            "old_lines": [],
            "new_lines": ["### message_echo", "- Received: Oct 20"],
        }],
    ), spec=_spec())

    payload = state_actor_markdown(state)

    assert "`message_echo` — Message from Echo" in payload
    assert "### message_echo\n- Received: Oct 20" in payload
    assert payload.index("## Currently visible targets") < payload.index(
        "## Continuous target-oriented memory"
    )
    visible_block = payload[
        payload.index("## Currently visible targets"):
        payload.index("## Continuous target-oriented memory")
    ]
    assert "  - Received: Oct 20" in visible_block
    assert "Order has no spatial or priority meaning" in payload
    assert "unobserved, not absent" in payload
    for forbidden in (
        "predicate_fact_pairs", "accepted", "resolved", "coverage",
    ):
        assert forbidden not in payload


def test_state_projection_keeps_current_observed_target_order() -> None:
    initial = reduce_worker_state(None, _batch(
        mode="init",
        frame_id="frame:1",
        visible_targets=[
            {
                "target_ref": "record_b",
                "identity": "Record B",
                "visibility": "full",
                "owned_region_visibility": "unobscured",
            },
            {
                "target_ref": "record_a",
                "identity": "Record A",
                "visibility": "full",
                "owned_region_visibility": "unobscured",
            },
        ],
        edits=[{
            "old_lines": [],
            "new_lines": ["### record_a", "- Value: A", "", "### record_b", "- Value: B"],
        }],
    ), spec=_spec())
    current = reduce_worker_state(initial, _batch(
        mode="edit",
        frame_id="frame:2",
        visible_targets=[
            {
                "target_ref": "record_a",
                "identity": "Record A",
                "visibility": "full",
                "owned_region_visibility": "unobscured",
            },
            {
                "target_ref": "record_b",
                "identity": "Record B",
                "visibility": "full",
                "owned_region_visibility": "unobscured",
            },
        ],
        edits=[],
    ), spec=_spec())

    payload = state_actor_markdown(current)
    visible = payload[:payload.index("## Continuous target-oriented memory")]
    assert list(current.targets) == ["record_a", "record_b"]
    assert visible.index("`record_a`") < visible.index("`record_b`")


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
    # State owns the goal-establishment judgment, so it sees the contract facts.
    assert focus["goal_contract"]["success_criteria"] == [
        "Set every target property to the requested value.",
    ]
    assert focus["goal_contract"]["completion_facts"] == [{
        "property_ref": "commit_observed",
        "description": "The requested commit is visibly established.",
        "expected_value": True,
    }]
    # But the filter predicate itself is not an observed fact shape.
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
