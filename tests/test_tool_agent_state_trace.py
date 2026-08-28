import pytest

from gui_agent.core.tool_agent.contracts import (
    WorkerSpec,
    WorkerStateEditBatch,
    WorkerStateUpdate,
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


def _update(**values) -> WorkerStateUpdate:
    return WorkerStateUpdate.model_validate({
        "status": "advance",
        "next_objective": "Set the visible target to the requested value.",
        "target_refs": [],
        "evidence": [],
        "rows": [],
        "surface": None,
        "visible_targets": [],
        **values,
    })


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


def test_state_bounds_verbose_current_frame_identity_without_repair() -> None:
    batch = _batch(
        mode="init",
        frame_id="frame:1",
        surface="Record list",
        visible_targets=[{
            "target_ref": "stable_record",
            "identity": "Stable visible record " + "detail " * 80,
            "visibility": "full",
            "owned_region_visibility": "unobscured",
        }],
        edits=[],
    )

    assert len(batch.visible_targets[0].identity) == 300
    assert batch.visible_targets[0].identity.startswith("Stable visible record")
    assert batch.visible_targets[0].identity.endswith("...")


def test_state_bounds_verbose_surface_without_repair() -> None:
    batch = _batch(
        mode="init",
        frame_id="frame:1",
        surface="Mastodon settings navigation menu " + "item " * 40,
        visible_targets=[],
        edits=[],
    )

    assert batch.surface is not None
    assert len(batch.surface) == 120
    assert batch.surface.endswith("...")


def test_state_atomically_updates_facts_and_current_task_transition() -> None:
    state = reduce_worker_state(None, _update(
        mode="init",
        frame_id="frame:1",
        surface="Record list",
        visible_targets=[{
            "target_ref": "pendingRecord",
            "identity": "Pending record",
            "visibility": "full",
            "owned_region_visibility": "unobscured",
        }],
        edits=[{
            "old_lines": [],
            "new_lines": ["### pending_record", "- Requested value: false"],
        }],
        target_refs=["pendingRecord"],
    ), spec=_spec())

    assert "Requested value: false" in state.markdown
    assert state.task_transition is not None
    assert state.task_transition.status == "advance"
    assert state.task_transition.target_refs == ["pendingRecord"]
    assert state_continuation_payload(state)["previous_task_transition"] == {
        "status": "advance",
        "next_objective": "Set the visible target to the requested value.",
        "target_refs": ["pendingRecord"],
    }
    projection = state_actor_markdown(state)
    assert "## Current task objective" in projection
    assert "`pendingRecord`" in projection


def test_state_update_rejects_incoherent_or_unavailable_transition() -> None:
    with pytest.raises(ValueError, match="advance requires"):
        _update(
            mode="init", frame_id="frame:1", edits=[], next_objective="",
        )
    with pytest.raises(ValueError, match="complete requires evidence"):
        _update(
            mode="init", frame_id="frame:1", edits=[], status="complete",
            next_objective="", target_refs=[],
        )
    with pytest.raises(ValueError, match="must be visible"):
        reduce_worker_state(None, _update(
            mode="init", frame_id="frame:1", edits=[],
            target_refs=["offscreen_record"],
        ), spec=_spec())


def test_state_can_record_decisive_fact_and_complete_in_one_update() -> None:
    initial = reduce_worker_state(None, _batch(
        mode="init", frame_id="frame:1",
        edits=[{"old_lines": [], "new_lines": ["### record", "- Value: false"]}],
    ), spec=_spec())
    completed = reduce_worker_state(initial, _update(
        mode="edit",
        frame_id="frame:2",
        edits=[{
            "old_lines": ["- Value: false"],
            "new_lines": ["- Value: true"],
        }],
        status="complete",
        next_objective="",
        target_refs=[],
        evidence=["The requested value is visibly true."],
    ), spec=_spec())

    assert completed.markdown.endswith("- Value: true")
    assert completed.task_transition is not None
    assert completed.task_transition.status == "complete"


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

    # An exact single-occurrence anchor is replaced in place.
    replaced = reduce_worker_state(initial, _batch(
        mode="edit", frame_id="frame:2",
        edits=[
            {"old_lines": ["### record"], "new_lines": ["### record"]},
            {"old_lines": ["- Value: one"], "new_lines": ["- Value: changed"]},
        ],
    ), spec=_spec())
    assert "- Value: changed" in replaced.markdown
    assert "- Value: one" not in replaced.markdown
    # An anchor that occurs more than once is ambiguous: preserve the new content by
    # appending instead of guessing which occurrence to replace.
    doubled = reduce_worker_state(None, _batch(
        mode="init", frame_id="frame:1",
        edits=[{"old_lines": [], "new_lines": ["### record", "- Value: one", "- Value: one"]}],
    ), spec=_spec())
    ambiguous = reduce_worker_state(doubled, _batch(
        mode="edit", frame_id="frame:2",
        edits=[{"old_lines": ["- Value: one"], "new_lines": ["replacement"]}],
    ), spec=_spec())
    assert "- Value: one" in ambiguous.markdown
    assert "replacement" in ambiguous.markdown
    # Empty old_lines against a non-empty memory appends the new observation.
    appended = reduce_worker_state(initial, _batch(
        mode="edit", frame_id="frame:2",
        edits=[{"old_lines": [], "new_lines": ["- Appended: two"]}],
    ), spec=_spec())
    assert "- Value: one" in appended.markdown
    assert "- Appended: two" in appended.markdown


def test_state_edit_with_drifted_anchor_appends_and_preserves_new_content() -> None:
    initial = reduce_worker_state(None, _batch(
        mode="init", frame_id="frame:1",
        edits=[{"old_lines": [], "new_lines": ["### record", "- Value: one"]}],
    ), spec=_spec())

    # When the anchor block is no longer present (memory drifted, model mis-reproduced
    # the exact text), the State observation is appended instead of aborting the run.
    state = reduce_worker_state(initial, _batch(
        mode="edit", frame_id="frame:2",
        edits=[{"old_lines": ["- Value: old"], "new_lines": ["- Value: two"]}],
    ), spec=_spec())
    markdown = state.markdown
    assert "- Value: one" in markdown
    assert "- Value: two" in markdown


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
    assert focus["goal_contract"]["goal"] == (
        "Set every target property to the requested value."
    )
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
