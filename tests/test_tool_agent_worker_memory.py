from __future__ import annotations

from types import SimpleNamespace

import pytest

from gui_agent.core.tool_agent.contracts import (
    MaterializedFrame,
    WorkerSpec,
    WorkerState,
)
from gui_agent.core.tool_agent.runtime import ToolAgentRuntime
from gui_agent.core.tool_agent.worker_memory import (
    WorkerJournal,
    build_current_state,
    build_worker_memory_view,
    build_progress_snapshot,
    memory_repair_instruction,
    project_worker_context,
)


def test_runtime_owned_commitment_lifecycle_reduces_without_worker_updates() -> None:
    journal = WorkerJournal(worker_id="runtime_commitment")
    dispatched = journal.record_runtime_commitment(
        step=1, substep=1, frame_id="frame:1", tool="tap",
        statement="Activate the verified control", status="dispatched",
    )
    journal.record_action_result(
        step=1, substep=1, frame_id="frame:1", tool="tap",
        args={"description": "Activate the verified control"},
        result={"status": "executed", "action_type": "tap", "no_effect": False},
        commitment_refs=(dispatched.event_ref,),
    )
    settled = journal.settle_runtime_commitment(
        step=1, substep=1, frame_id="frame:1", tool="tap",
        statement="Activate the verified control",
        result={"status": "executed", "action_type": "tap", "no_effect": False},
    )

    assert dispatched.origin == settled.origin == "runtime"
    assert settled.status == "satisfied"
    progress = build_progress_snapshot(build_worker_memory_view(journal))
    assert progress.commitments["active"] == ()
    assert progress.commitments["satisfied"] == ("commitment:action_1_1",)
    assert dispatched.event_ref in progress.audit_refs


def test_progress_snapshot_is_bounded_independently_of_journal_length() -> None:
    journal = WorkerJournal(worker_id="bounded_progress")
    for step in range(1, 40):
        journal.record_memory_updates(
            step=step, frame_id=f"frame:{step}",
            state=_memory_state({
                "fact_type": "evidence", "key": f"fact_{step}",
                "status": "active", "lifetime": "attempt",
                "statement": f"Verified fact {step}", "depends_on": [],
            }),
        )
    progress = build_progress_snapshot(build_worker_memory_view(journal))

    assert len(progress.effects) == 16
    assert len(progress.audit_refs) == 16
    assert progress.effects[-1]["statement"] == "Verified fact 39"


def _state(step: int) -> WorkerState:
    return WorkerState(
        status="exploring",
        summary=f"Observed state {step}",
        memory_updates=[],
    )


def test_staged_memory_is_not_visible_before_runtime_commit() -> None:
    journal = WorkerJournal(worker_id="staged_action_memory")
    state = _memory_state({
        "fact_type": "evidence", "key": "action_intent",
        "status": "active", "lifetime": "attempt",
        "statement": "The action is ready", "depends_on": [],
    })

    pending = journal.stage_memory_updates(
        step=1, frame_id="frame:1", state=state,
    )

    assert journal.events == []
    journal.commit_memory_updates(pending)
    assert journal.events[-1].fact_ref == "evidence:action_intent"


def test_target_verified_action_records_durable_runtime_evidence() -> None:
    journal = WorkerJournal(worker_id="receipt_order")
    journal.record_action_result(
        step=1, frame_id="frame:1", tool="tap",
        args={"description": "Activate the requested setting"},
        result={
            "status": "executed", "action_type": "tap", "no_effect": False,
            "target_signal": {
                "status": "on_target", "actual_element": "Requested setting",
                "container_context": "Account row: Ada Lovelace",
            },
        },
    )

    evidence = build_worker_memory_view(
        journal, current_frame_id="frame:2",
    ).accumulated_evidence
    assert [event.fact_ref for event in evidence] == ["evidence:verified_action_1"]
    assert evidence[0].origin == "runtime"
    assert "actual_target='Requested setting'" in evidence[0].statement
    transaction = build_worker_memory_view(journal).target_transaction
    assert transaction is not None
    assert transaction.target.container_context == "Account row: Ada Lovelace"
    assert evidence[0].target_ref == transaction.target.ref


def test_target_transaction_binds_overlay_evidence_and_exact_return() -> None:
    journal = WorkerJournal(worker_id="target_transaction")
    journal.record_action_result(
        step=1, frame_id="frame:1", surface_fingerprint="anchor",
        tool="tap", args={"x": 900, "y": 300, "description": "Open row actions"},
        result={
            "status": "executed", "action_type": "tap", "no_effect": False,
            "target_signal": {
                "status": "on_target", "actual_element": "More options",
                "container_context": "Ada — quarterly report",
            },
        },
    )
    target_ref = journal.active_target.ref  # type: ignore[union-attr]
    pending = journal.stage_memory_updates(
        step=2, frame_id="frame:2", state=_memory_state({
            "fact_type": "evidence", "key": "row_state",
            "status": "active", "lifetime": "attempt",
            "statement": "The row menu shows the inverse action", "depends_on": [],
        }),
    )
    journal.record_action_result(
        step=2, frame_id="frame:2", surface_fingerprint="overlay",
        tool="back", args={"description": "Dismiss the row menu"},
        result={"status": "executed", "action_type": "back", "no_effect": False},
    )
    journal.commit_memory_updates(pending, target_ref=target_ref)

    active = build_worker_memory_view(
        journal, current_frame_id="frame:2", current_surface_fingerprint="overlay",
    ).target_transaction
    returned = build_worker_memory_view(
        journal, current_frame_id="frame:3", current_surface_fingerprint="anchor",
    ).target_transaction

    assert active is not None and active.status == "active"
    assert returned is not None and returned.status == "returned_to_anchor"
    assert [event.key for event in returned.evidence] == [
        "verified_action_1", "verified_action_2", "row_state",
    ]
    projection = project_worker_context(
        memory=build_worker_memory_view(
            journal, current_frame_id="frame:3", current_surface_fingerprint="anchor",
        ),
        frame=MaterializedFrame(
            frame_id="frame:3", screenshot_path="frame.png",
            visual_fingerprint="anchor",
        ),
    ).text
    assert '"phase": "returned_to_anchor"' in projection
    assert "The row menu shows the inverse action" in projection
    assert '"phase": "worker_decision_required"' in projection
    assert projection.rindex("## Current State") > projection.index(
        "## Historical Progress"
    )


def test_changed_surface_never_claims_target_return() -> None:
    journal = WorkerJournal(worker_id="target_changed_surface")
    journal.record_action_result(
        step=1, frame_id="frame:1", surface_fingerprint="anchor",
        tool="tap", args={"x": 100, "y": 200},
        result={
            "status": "executed", "action_type": "tap", "no_effect": False,
            "target_signal": {
                "status": "on_target", "actual_element": "Actions",
                "container_context": "Record alpha",
            },
        },
    )

    transaction = build_worker_memory_view(
        journal, current_frame_id="frame:2", current_surface_fingerprint="different",
    ).target_transaction

    assert transaction is not None
    assert transaction.status == "active"


def test_verified_spatial_target_does_not_require_semantic_container_context() -> None:
    journal = WorkerJournal(worker_id="coordinate_target")
    journal.record_action_result(
        step=1, frame_id="frame:1", surface_fingerprint="anchor",
        tool="tap", args={"x": 420, "y": 240},
        result={
            "status": "executed", "action_type": "tap", "no_effect": False,
            "target_signal": {
                "status": "on_target", "actual_element": "More options",
            },
        },
    )

    assert journal.active_target is not None
    assert journal.active_target.container_context == ""
    assert journal.active_target.point == (420.0, 240.0)


def test_verified_target_on_new_surface_starts_new_transaction() -> None:
    journal = WorkerJournal(worker_id="new_surface_target")
    journal.record_action_result(
        step=1, frame_id="frame:1", surface_fingerprint="first",
        tool="tap", args={"x": 100, "y": 200},
        result={
            "status": "executed", "action_type": "tap", "no_effect": False,
            "target_signal": {"status": "on_target", "actual_element": "Profile"},
        },
    )
    first_ref = journal.active_target.ref  # type: ignore[union-attr]

    journal.record_action_result(
        step=2, frame_id="frame:2", surface_fingerprint="second",
        tool="tap", args={"x": 900, "y": 300},
        result={
            "status": "executed", "action_type": "tap", "no_effect": False,
            "target_signal": {"status": "on_target", "actual_element": "More options"},
        },
    )

    assert journal.active_target is not None
    assert journal.active_target.ref != first_ref
    assert journal.active_target.anchor_surface_fingerprint == "second"
    assert journal.latest_action_receipt is not None
    assert journal.latest_action_receipt.target_ref == journal.active_target.ref


def test_effectful_traversal_clears_target_but_no_effect_does_not() -> None:
    journal = WorkerJournal(worker_id="target_traversal")
    journal.record_action_result(
        step=1, frame_id="frame:1", surface_fingerprint="anchor",
        tool="tap", args={"x": 100, "y": 200},
        result={
            "status": "executed", "action_type": "tap", "no_effect": False,
            "target_signal": {
                "status": "on_target", "actual_element": "Actions",
                "container_context": "Record alpha",
            },
        },
    )
    journal.record_action_result(
        step=2, frame_id="frame:2", surface_fingerprint="anchor",
        tool="scroll", args={"direction": "down"},
        result={"status": "executed", "action_type": "scroll", "no_effect": True},
    )
    assert journal.active_target is not None

    journal.record_action_result(
        step=3, frame_id="frame:3", surface_fingerprint="anchor",
        tool="scroll", args={"direction": "down"},
        result={"status": "executed", "action_type": "scroll", "no_effect": False},
    )
    assert journal.active_target is None


def test_executed_back_records_runtime_effect_without_claiming_a_target() -> None:
    journal = WorkerJournal(worker_id="back_receipt")
    journal.record_action_result(
        step=2, frame_id="frame:2", tool="back",
        args={"description": "Dismiss the open menu"},
        result={"status": "executed", "action_type": "back", "no_effect": False},
    )

    evidence = build_worker_memory_view(
        journal, current_frame_id="frame:3",
    ).accumulated_evidence
    assert [event.fact_ref for event in evidence] == ["evidence:verified_action_2"]
    assert "intent='Dismiss the open menu'" in evidence[0].statement
    assert "tool='back'" in evidence[0].statement
    assert "actual_target" not in evidence[0].statement


def _record_turn(
    journal: WorkerJournal,
    *,
    step: int,
    frame_id: str,
    state: WorkerState,
    tool: str,
    args: dict,
    result: dict,
) -> None:
    journal.record_memory_updates(step=step, frame_id=frame_id, state=state)
    journal.record_action_result(
        step=step,
        frame_id=frame_id,
        tool=tool,
        args=args,
        result=result,
    )




def test_worker_context_omits_unavailable_structured_controls() -> None:
    frame = MaterializedFrame(
        frame_id="frame:android",
        screenshot_path="android.png",
        visual_fingerprint="runtime-private-surface",
        controls=[],
    )

    projection = project_worker_context(
        memory=build_worker_memory_view(WorkerJournal(worker_id="android")),
        frame=frame,
    )

    assert '"controls"' not in projection.text
    assert "runtime-private-surface" not in projection.text





def test_worker_context_always_uses_compact_semantic_frame() -> None:
    journal = WorkerJournal(worker_id="collector")
    _record_turn(
        journal, step=1, frame_id="frame:1", state=_state(1), tool="tap",
        args={"description": "Invoke a non-visual control"},
        result={"status": "executed", "action_type": "tap", "no_effect": True},
    )
    frame = MaterializedFrame.model_validate({
        "frame_id": "frame:2",
        "screenshot_path": "frame.png",
        "readiness": "blank",
        "readiness_reason": "no visible structure",
        "controls": [{
            "kind": "button",
            "label": "Apply",
            "id": "private-dom-id",
            "name": "private-control-name",
            "group_id": "private-group-id",
            "form_action": "commit",
            "rect": {"x": 100, "y": 200, "w": 80, "h": 30},
        }],
        "collections": [{
            "ref": "collection:records",
            "requirement_id": "records",
            "chunk_refs": ["chunk:records:1"],
            "row_count": 10,
            "row_schema": {
                "type": "object",
                "properties": {
                    "value": {
                        "type": "string",
                        "description": "x" * 10_000,
                    },
                },
            },
            "coverage": {"status": "complete", "scope_status": "met"},
        }],
    })

    projection = project_worker_context(
        memory=build_worker_memory_view(journal, current_frame_id="frame:2"),
        frame=frame,
        application_knowledge="The invoked control has a non-visual postcondition.",
        attempt_contract=(
            "## Current Worker attempt\n"
            '{"approach": "Inspect the requested records"}'
        ),
        max_chars=4_000,
    )

    frame_decision = next(
        item for item in projection.report["blocks"]
        if item["id"] == "tool_agent.worker.04_current_state"
    )
    assert frame_decision["action"] == "kept"
    assert "collection:records" in projection.text
    assert '"readiness": "blank"' in projection.text
    assert '"readiness_reason": "no visible structure"' in projection.text
    assert "x" * 100 not in projection.text
    assert "private-dom-id" not in projection.text
    assert "private-control-name" not in projection.text
    assert "private-group-id" not in projection.text
    assert '"form_action": "commit"' in projection.text
    assert '"rect": {"x": 100, "y": 200, "w": 80, "h": 30}' in projection.text
    assert projection.text.index("## Static Rules") < projection.text.index(
        "## Goal Contract"
    ) < projection.text.index("## Historical Progress") < projection.text.index(
        "## Current State"
    )
    assert next(
        item for item in projection.report["blocks"]
        if item["id"] == "tool_agent.worker.02_goal_contract"
    )["action"] == "kept"
    knowledge = next(
        item for item in projection.report["blocks"]
        if item["id"] == "tool_agent.worker.01_static_rules"
    )
    assert (knowledge["source_type"], knowledge["ttl"]) == (
        "runtime_contract", "session",
    )


def test_worker_context_hides_offscreen_inventory_but_keeps_choice_state() -> None:
    frame = MaterializedFrame(
        frame_id="frame:detail",
        screenshot_path="frame.png",
        requirement_scopes={
            "products": {
                "status": "unknown",
                "detail_resolution": {
                    "status": "active",
                    "current_observed_detail_fields": ["material"],
                },
            },
        },
        controls=[
            {"kind": "button", "label": "Back", "in_viewport": True},
            *[
                {
                    "kind": "a",
                    "label": f"Edit row {index}",
                    "value": "Edit",
                    "in_viewport": False,
                }
                for index in range(200)
            ],
            {
                "kind": "native_select",
                "label": "Material",
                "selected_text": "",
                "in_viewport": False,
                "viewport_pos": "below",
            },
            {
                "kind": "checkbox",
                "label": "alex",
                "value": False,
                "selected": False,
                "selection_mode": "multiple",
                "in_viewport": True,
                "rect": {"x": 500, "y": 243, "w": 1000, "h": 61},
                "action_point": {"x": 918, "y": 241},
            },
        ],
    )

    projection = project_worker_context(
        memory=build_worker_memory_view(WorkerJournal(worker_id="detail")),
        frame=frame,
    )

    assert '"label": "Material"' in projection.text
    assert '"selected_text": ""' in projection.text
    assert "Edit row" not in projection.text
    assert '"observed_choice_state"' in projection.text
    observed = projection.text.split('"observed_choice_state"', 1)[1]
    assert '"in_viewport"' not in observed.split('"structured_surfaces"', 1)[0]
    controls = projection.text.split('"controls"', 1)[1]
    assert '"label": "Material"' not in controls
    assert '"label": "alex"' in controls
    assert '"selected": false' in controls
    assert '"action_point"' not in controls
    assert projection.text.index('"requirement_scopes"') < projection.text.index('"controls"')
    assert len(projection.text) < 5_000


def test_worker_context_keeps_structured_surface_completion_evidence() -> None:
    frame = MaterializedFrame(
        frame_id="frame:2",
        screenshot_path="frame.png",
        url="https://example.test/report/filter/encoded/",
        title="Orders Report",
        applied_filters={"From": "5/1/21", "To": "3/31/22"},
        structured_surfaces=[{
            "kind": "rendered_data_surface",
            "rendered": True,
            "caption": "Results",
            "fields": ["Interval", "Orders", "Sales Total"],
            "row_count": 33,
            "total_records": 33,
            "partial": False,
        }],
    )

    projection = project_worker_context(
        memory=build_worker_memory_view(WorkerJournal(worker_id="operator")),
        frame=frame,
        max_chars=4_000,
    )

    assert '"structured_surfaces"' in projection.text
    assert '"row_count": 33' in projection.text
    assert '"From": "5/1/21"' in projection.text


def test_worker_context_projects_extra_filter_as_authoritative_scope_blocker() -> None:
    frame = MaterializedFrame(
        frame_id="frame:3",
        screenshot_path="frame.png",
        controls=[{"kind": "button", "label": "Clear all"}],
        applied_filters={
            "Status": "Complete",
            "Purchase Date": "1/01/2023 - 5/31/2023",
        },
        requirement_scopes={
            "completed_orders": {
                "status": "unmet",
                "requested_filters": {"Status": "Complete"},
                "applied_filters": {
                    "Status": "Complete",
                    "Purchase Date": "1/01/2023 - 5/31/2023",
                },
            },
        },
        missing_requirements=["completed_orders"],
    )

    projection = project_worker_context(
        memory=build_worker_memory_view(WorkerJournal(worker_id="collector")),
        frame=frame,
    )

    assert '"status": "unmet"' in projection.text
    assert '"extra_applied_filters": ["Purchase Date"]' in projection.text
    assert '"instruction"' not in projection.text


class _RecordingWorker:
    def __init__(self) -> None:
        self.calls: list[list[object]] = []
        self.bind_kwargs: list[dict[str, object]] = []
        self.step = 0

    def bind_tools(self, tools, **kwargs):
        del tools
        self.bind_kwargs.append(dict(kwargs))
        return self

    def invoke(self, messages):
        self.calls.append(list(messages))
        self.step += 1
        return SimpleNamespace(
            content="",
            tool_calls=[{
                "id": f"tap-{self.step}",
                "name": "runtime_tap_visible",
                "args": {
                    "state": {
                        "status": "exploring",
                        "summary": f"Observed state {self.step}",
                        "memory_updates": [],
                    },
                    "x": 100 + self.step * 30,
                    "y": 200 + self.step * 30,
                    "description": f"Advance step {self.step}",
                },
            }],
        )


class _Executor:
    def execute(self, decision, **kwargs):
        del decision, kwargs
        return True


def _human_text(messages: list[object]) -> str:
    content = getattr(messages[-1], "content", [])
    return "\n".join(
        str(item.get("text") or "")
        for item in content
        if isinstance(item, dict) and item.get("type") == "text"
    )


def test_worker_rebuilds_fresh_messages_each_frame_instead_of_replaying_chat_history(
    monkeypatch,
) -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.trace = []
    runtime.worker = _RecordingWorker()
    runtime._executor = _Executor()
    runtime.platform = object()
    frames = iter(range(1, 4))
    def observe(_spec):
        runtime._frame_no += 1
        return (
            MaterializedFrame(
                frame_id=f"frame:{next(frames)}",
                screenshot_path="frame.png",
            ),
            b"png",
        )

    runtime._observe = observe
    monkeypatch.setattr(
        "gui_agent.core.tool_agent.runtime.settle_after_action",
        lambda platform, png, **_kwargs: (0.0, False),
    )
    spec = WorkerSpec(
        goal="Advance through three visual states",
        success_criteria=["The final state is visible"],
        strategy={"approach": "Advance through the visible states."},
    )
    runtime._platform_capabilities = frozenset({"scroll"})
    runtime.max_turns = 3
    runtime._frame_no = 0

    outcome = runtime._run_worker("bounded_worker", spec)

    assert outcome.phase == "failed"
    assert len(runtime.worker.calls) == 3
    assert [len(messages) for messages in runtime.worker.calls] == [2, 2, 2]
    assert [
        [getattr(message, "type", "") for message in messages]
        for messages in runtime.worker.calls
    ] == [["system", "human"]] * 3
    assert '"latest_transition_ref": ""' in _human_text(runtime.worker.calls[0])
    assert "step:1" in _human_text(runtime.worker.calls[1])
    assert "step:2" in _human_text(runtime.worker.calls[2])
    assert all(
        sum(
            1
            for item in getattr(messages[-1], "content", [])
            if isinstance(item, dict) and item.get("type") == "image_url"
        ) == 1
        for messages in runtime.worker.calls
    )
    decisions = [
        event for event in runtime.trace if event["event"] == "worker_decision"
    ]
    assert [event["memory_event_count"] for event in decisions] == [0, 1, 2]
    assert all(
        kwargs["tool_choice"] == "required"
        and kwargs["extra_body"] == {"enable_thinking": False}
        for kwargs in runtime.worker.bind_kwargs
    )
    assert all(event["context_chars"] > 0 for event in decisions)
    human_log = runtime._human_line(decisions[-1])
    assert "Context    : rebuilt for frame" in human_log
    assert "2 journal events" in human_log
    assert all(
        any(
            report.get("kind") == "context_compression"
            for report in event["context_reports"]
        )
        for event in decisions
    )


def _memory_state(*updates: dict) -> WorkerState:
    return WorkerState.model_validate({
        "status": "collecting",
        "summary": "Typed memory update",
        "memory_updates": list(updates),
    })



def test_observation_expires_with_frame_and_evidence_is_attempt_scoped() -> None:
    journal = WorkerJournal(worker_id="scope_memory")
    journal.record_memory_updates(
        step=1,
        frame_id="frame:1",
        state=_memory_state({
            "fact_type": "observation", "key": "rename_absent",
            "status": "active", "lifetime": "frame",
            "statement": "The menu does not contain Rename", "depends_on": [],
        }),
    )
    journal.record_memory_updates(
        step=2,
        frame_id="frame:1",
        state=_memory_state({
            "fact_type": "evidence", "key": "rename_path_unavailable",
            "status": "active", "lifetime": "attempt",
            "statement": "The inspected menu has no Rename action", "depends_on": [],
        }),
    )

    current = build_worker_memory_view(journal, current_frame_id="frame:1")
    later = build_worker_memory_view(journal, current_frame_id="frame:2")

    assert [event.key for event in current.current_observations] == ["rename_absent"]
    assert later.current_observations == ()
    assert [event.key for event in later.accumulated_evidence] == [
        "rename_path_unavailable"
    ]




def test_observation_lifetime_is_structurally_enforced() -> None:
    with pytest.raises(ValueError, match="lifetime='frame'"):
        _memory_state({
            "fact_type": "observation", "key": "current_location",
            "status": "active", "lifetime": "attempt",
            "statement": "The current location is Documents", "depends_on": [],
        })


def test_ask_user_preserves_observation_window_but_gui_action_expires_it() -> None:
    journal = WorkerJournal(worker_id="observation_window")
    journal.record_memory_updates(
        step=1,
        frame_id="frame:1",
        state=_memory_state({
            "fact_type": "observation", "key": "current_parent",
            "status": "active", "lifetime": "frame",
            "statement": "The new-item dialog is open under Root/team", "depends_on": [],
        }),
    )
    journal.record_action_result(
        step=1,
        frame_id="frame:1",
        tool="ask_user",
        args={"question": "Which exact child is required?"},
        result={"status": "executed", "action_type": "ask_user"},
    )

    same_window = build_worker_memory_view(journal, current_frame_id="frame:2")
    assert [event.key for event in same_window.current_observations] == [
        "current_parent"
    ]

    journal.record_action_result(
        step=2,
        frame_id="frame:2",
        tool="type",
        args={"text": "archive"},
        result={"status": "executed", "action_type": "type"},
    )
    changed_window = build_worker_memory_view(journal, current_frame_id="frame:3")
    assert changed_window.current_observations == ()


def test_current_state_exposes_answered_user_input_without_semantic_guard() -> None:
    journal = WorkerJournal(worker_id="answered_input")
    question = "Which exact destination should be used?"
    journal.record_action_result(
        step=1,
        frame_id="frame:1",
        tool="ask_user",
        args={"question": question},
        result={"status": "executed", "action_type": "ask_user"},
    )
    memory = build_worker_memory_view(journal, current_frame_id="frame:2")
    state = build_current_state(
        frame=MaterializedFrame(frame_id="frame:2", screenshot_path="frame.png"),
        observation={},
        memory=memory,
        spec=None,
        completion_mode="operator",
        same_frame_feedback=None,
    )

    assert state.user_input == {
        "status": "answered",
        "requested_questions": (question,),
        "request_count_in_recent_window": 1,
        "instruction": (
            "Consume the recorded authoritative response. Do not ask any listed "
            "question again; a refusal or unavailable value is still a resolved "
            "request and requires another UI action or report_blocked."
        ),
    }
    with pytest.raises(ValueError, match="requires lifetime='attempt'"):
        _memory_state({
            "fact_type": "evidence", "key": "verified_identity",
            "status": "active", "lifetime": "frame",
            "statement": "The exact selected identity is record-7", "depends_on": [],
        })



def test_phase_label_does_not_gate_memory_updates() -> None:
    journal = WorkerJournal(worker_id="execution_phase")
    invalid = WorkerState.model_validate({
        "status": "executing",
        "summary": "Start execution without a commitment",
        "memory_updates": [{
            "fact_type": "evidence", "key": "candidate_observed",
            "status": "active", "lifetime": "attempt",
            "statement": "One candidate was observed", "depends_on": [],
        }],
    })

    journal.record_memory_updates(step=1, frame_id="frame:1", state=invalid)
    assert journal.events[-1].fact_ref == "evidence:candidate_observed"





def test_worker_cannot_modify_runtime_owned_memory() -> None:
    journal = WorkerJournal(worker_id="runtime_ownership")
    journal.record_runtime_input(
        key="user_response_2",
        statement="The exact destination is Root/team/archive",
    )
    before = tuple(journal.events)

    for status in ("active", "retracted"):
        with pytest.raises(ValueError, match="Runtime-owned memory"):
            journal.record_memory_updates(
                step=3,
                frame_id="frame:3",
                state=_memory_state({
                    "fact_type": "evidence", "key": "user_response_2",
                    "status": status, "lifetime": "attempt",
                    "statement": "A Worker-authored replacement", "depends_on": [],
                }),
            )

    assert tuple(journal.events) == before


def test_memory_retraction_is_versioned_and_atomic() -> None:
    journal = WorkerJournal(worker_id="atomic_memory")
    journal.record_memory_updates(
        step=1,
        frame_id="frame:1",
        state=_memory_state({
            "fact_type": "evidence", "key": "record_match",
            "status": "active", "lifetime": "attempt",
            "statement": "Record A matches", "depends_on": [],
        }),
    )
    before = tuple(journal.events)
    try:
        journal.record_memory_updates(
            step=2,
            frame_id="frame:2",
            state=_memory_state(
                {
                    "fact_type": "evidence", "key": "other_match",
                    "status": "active", "lifetime": "attempt",
                    "statement": "Record B matches", "depends_on": [],
                },
                {
                    "fact_type": "claim", "key": "bad_claim",
                    "status": "active", "lifetime": "attempt",
                    "statement": "An unsupported conclusion",
                    "depends_on": ["evidence:missing"],
                },
            ),
        )
    except ValueError as exc:
        assert "observation" in str(exc) and "evidence" in str(exc)
    else:
        raise AssertionError("invalid dependency must reject the whole delta")
    assert tuple(journal.events) == before

    journal.record_memory_updates(
        step=3,
        frame_id="frame:3",
        state=_memory_state({
            "fact_type": "evidence", "key": "record_match",
            "status": "retracted", "lifetime": "attempt",
            "statement": "Record A was corrected", "depends_on": [],
        }),
    )
    assert build_worker_memory_view(journal).accumulated_evidence == ()
    assert journal.events[-1].supersedes == "step:1:memory:1"
