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
    build_worker_memory_view,
    project_worker_context,
)


def _state(step: int) -> WorkerState:
    return WorkerState(
        status="exploring",
        summary=f"Observed state {step}",
        memory_updates=[],
    )


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


def test_worker_memory_is_a_bounded_projection_of_append_only_runtime_facts() -> None:
    journal = WorkerJournal(worker_id="bounded_worker")
    for step in range(1, 13):
        _record_turn(
            journal,
            step=step,
            frame_id=f"frame:{step}",
            state=_state(step),
            tool="runtime_tap_visible",
            args={"x": step, "y": step},
            result={"status": "executed", "no_effect": step % 2 == 0},
        )

    memory = build_worker_memory_view(journal)

    assert [item.event_ref for item in memory.recent_receipts] == [
        "step:9",
        "step:10",
        "step:11",
        "step:12",
    ]
    rendered = memory.render_prompt_section()
    assert "Pending subgoal" not in rendered
    assert "invocation=confirmed" in rendered
    assert "screen_transition=none_observed" in rendered
    assert "unchanged screen alone does not justify repeating" in rendered
    assert "[step:1]" not in rendered
    assert "append-only event journal" in rendered


def test_worker_memory_omits_spatial_and_execution_metadata() -> None:
    journal = WorkerJournal(worker_id="compact_worker")
    _record_turn(
        journal,
        step=1,
        frame_id="volatile-frame:17",
        state=_state(1),
        tool="choose_status",
        args={
            "x": 410,
            "y": 520,
            "text": "Complete",
            "description": "Choose the Complete status in the upper filter area",
        },
        result={
            "status": "executed",
            "action_type": "select_option",
            "settle_seconds": 1.2,
            "no_effect": False,
            "grounding": {"x": 421, "y": 532, "source": "dom"},
            "target_signal": {
                "status": "off_target",
                "actual_element": "Status label",
            },
        },
    )

    rendered = build_worker_memory_view(journal).render_prompt_section()

    assert "volatile-frame:17" not in rendered
    assert '"x"' not in rendered
    assert '"y"' not in rendered
    assert "settle_seconds" not in rendered
    assert "grounding" not in rendered
    assert "Complete" in rendered
    assert "select_option" in rendered
    assert "flash verifier reported off_target" in rendered
    assert "Status label" in rendered
    assert "do not repeat the same point" in rendered


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


def test_worker_memory_preserves_flash_off_target_signal() -> None:
    journal = WorkerJournal(worker_id="target_feedback")
    _record_turn(
        journal,
        step=1,
        frame_id="frame:1",
        state=_state(1),
        tool="runtime_tap_visible",
        args={
            "x": 500,
            "y": 870,
            "description": "Tap Create New Channel",
        },
        result={
            "status": "executed",
            "action_type": "tap",
            "no_effect": False,
            "target_signal": {
                "status": "off_target",
                "actual_element": "Browse Channels",
                "reason": "The marker is on the adjacent row.",
            },
        },
    )

    rendered = build_worker_memory_view(journal).render_prompt_section()

    assert "flash verifier reported off_target" in rendered
    assert "Browse Channels" in rendered
    assert "do not repeat the same point" in rendered


def test_worker_memory_accumulates_explicit_visual_facts() -> None:
    journal = WorkerJournal(worker_id="identity_memory")
    for step, key, fact in (
        (1, "pupper_record", "Bookmarks identity: author=pupper; content=Border Collie"),
        (2, "demo_record", "Bookmarks identity: author=demo; content=Golden Retriever"),
        (3, "pupper_record", "Bookmarks identity: author=pupper; content=Border Collie; verified"),
    ):
        _record_turn(
            journal,
            step=step,
            frame_id=f"frame:{step}",
            state=WorkerState.model_validate({
                "status": "collecting",
                "summary": f"Observed state {step}",
                "memory_updates": [{
                    "fact_type": "evidence",
                    "key": key,
                    "status": "active",
                    "lifetime": "attempt",
                    "statement": fact,
                    "depends_on": [],
                }],
            }),
            tool="scroll_records",
            args={},
            result={"status": "executed", "action_type": "scroll", "no_effect": False},
        )

    rendered = build_worker_memory_view(journal).render_prompt_section()

    assert "author=pupper; content=Border Collie" in rendered
    assert "author=demo; content=Golden Retriever" in rendered
    assert len(build_worker_memory_view(journal).accumulated_evidence) == 2
    latest = next(
        event for event in build_worker_memory_view(journal).accumulated_evidence
        if event.key == "pupper_record"
    )
    assert latest.supersedes == "step:1:memory:1"
    assert latest.sequence == 5


def test_worker_memory_keeps_collection_cells_out_of_durable_memory() -> None:
    journal = WorkerJournal("collection_memory")
    frame = MaterializedFrame(
        frame_id="frame:7",
        screenshot_path="frame.png",
        controls=[{
            "label": "Bookmarks",
            "selected": True,
            "rect": {"x": 400, "y": 290, "w": 200, "h": 40},
        }, {
            "label": "Selected row",
            "selected": True,
            "selection_mode": "multiple",
            "rect": {"x": 400, "y": 400, "w": 200, "h": 40},
        }, {
            "label": "Profile",
            "selected": True,
            "rect": {"x": 875, "y": 930, "w": 250, "h": 80},
        }],
        visible_collection_regions=[{
            "bounds": (0, 330, 1000, 886),
            "cells": [
                {"ref": "body", "texts": ["Exact content 💛🐾"]},
                {"ref": "media", "texts": ["Media without description"]},
                {"ref": "actions", "texts": ["Reply", "Favorite"], "clipped_bottom": True},
            ],
        }],
    )

    rendered = build_worker_memory_view(journal).render_prompt_section()

    assert "Exact content 💛🐾" not in rendered
    assert journal.events == []
    projection = project_worker_context(
        memory=build_worker_memory_view(journal), frame=frame,
    )
    assert "Exact content 💛🐾" in projection.text


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
        if item["id"] == "tool_agent.worker.current_frame"
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
    assert projection.text.index("## Current Worker attempt") < projection.text.index(
        "## Current MaterializedFrame"
    ) < projection.text.index("## WorkerMemory") < projection.text.index(
        "## Application knowledge"
    )
    assert next(
        item for item in projection.report["blocks"]
        if item["id"] == "tool_agent.worker.current_attempt"
    )["action"] == "kept"
    knowledge = next(
        item for item in projection.report["blocks"]
        if item["id"] == "tool_agent.worker.application_knowledge"
    )
    assert (knowledge["source_type"], knowledge["ttl"]) == (
        "application_knowledge", "session",
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
    assert "No prior Worker actions" in _human_text(runtime.worker.calls[0])
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


def _record_dependency_chain(
    journal: WorkerJournal,
    *,
    source_type: str,
    source_key: str,
    claim_key: str,
    commitment_key: str,
) -> None:
    journal.record_memory_updates(
        step=1,
        frame_id="frame:1",
        state=WorkerState.model_validate({
            "status": "executing",
            "summary": "Establish the execution dependency chain",
            "memory_updates": [
                {
                    "fact_type": source_type, "key": source_key,
                    "status": "active",
                    "lifetime": "frame" if source_type == "observation" else "attempt",
                    "statement": "The source fact is established", "depends_on": [],
                },
                {
                    "fact_type": "claim", "key": claim_key,
                    "status": "active", "lifetime": "attempt",
                    "statement": "The execution claim is established",
                    "depends_on": [f"{source_type}:{source_key}"],
                },
                {
                    "fact_type": "commitment", "key": commitment_key,
                    "status": "active", "lifetime": "attempt",
                    "statement": "Execute the established claim",
                    "depends_on": [f"claim:{claim_key}"],
                },
            ],
        }),
    )


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
    rendered = later.render_prompt_section()
    assert "source=frame:1" in rendered
    assert "only the current frame proves present visibility or actionability" in rendered


def test_observation_dependency_invalidates_claim_and_commitment_next_frame() -> None:
    journal = WorkerJournal(worker_id="frame_local_claim")
    _record_dependency_chain(
        journal, source_type="observation", source_key="control_visible",
        claim_key="control_actionable", commitment_key="activate_control",
    )

    current = build_worker_memory_view(journal, current_frame_id="frame:1")
    later = build_worker_memory_view(journal, current_frame_id="frame:2")

    assert [event.key for event in current.established_claims] == ["control_actionable"]
    assert [event.key for event in current.active_commitments] == ["activate_control"]
    assert later.established_claims == ()
    assert later.active_commitments == ()
    rendered = later.render_prompt_section()
    assert "event_status=active; now=expired" in rendered
    assert "event_status=active; now=invalidated" in rendered


def test_latest_gui_transition_is_separated_from_earlier_receipts() -> None:
    journal = WorkerJournal(worker_id="post_commit_transition")
    journal.record_action_result(
        step=1, frame_id="frame:1", tool="tap",
        args={"description": "Focus the editor"},
        result={"status": "executed", "action_type": "tap"},
    )
    journal.record_action_result(
        step=2, substep=1, frame_id="frame:2", tool="type",
        args={"description": "Enter the requested value", "text": "requested value"},
        result={"status": "error", "error": "predispatch target rejected"},
    )
    journal.record_action_result(
        step=2, substep=1, frame_id="frame:2", tool="type",
        args={"description": "Enter the requested value", "text": "requested value"},
        result={"status": "executed", "action_type": "type", "no_effect": True},
    )
    journal.record_action_result(
        step=2, substep=2, frame_id="frame:2", tool="tap",
        args={"description": "Activate the final commit"},
        result={
            "status": "executed", "action_type": "tap",
            "target_signal": {
                "status": "on_target", "actual_element": "Activate the final commit",
            },
        },
    )

    memory = build_worker_memory_view(journal, current_frame_id="frame:3")
    rendered = memory.render_prompt_section()

    assert [event.event_ref for event in memory.latest_gui_transition] == [
        "step:2.1", "step:2.2",
    ]
    assert [event.event_ref for event in memory.recent_receipts] == [
        "step:1", "step:2.1", "step:2.2",
    ]
    assert "Latest GUI transition (immediately before the current frame)" in rendered
    assert "Activate the final commit" in rendered
    assert "Earlier execution receipts" in rendered
    assert "predispatch target rejected" not in rendered
    assert '"text": "requested value"' in rendered
    assert "invocation=confirmed; screen_transition=none_observed" in rendered
    assert rendered.count("[t=4; step:2.2]") == 1
    assert "No Commitment was bound to the latest invocation" in rendered
    assert "do not complete or invent a Commitment" in rendered

    journal.record_action_result(
        step=3, frame_id="frame:3", tool="ask_user", args={},
        result={"status": "executed"},
    )
    after_user_input = build_worker_memory_view(journal, current_frame_id="frame:4")
    assert after_user_input.latest_gui_transition == ()


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
    with pytest.raises(ValueError, match="require lifetime='attempt'"):
        _memory_state({
            "fact_type": "evidence", "key": "verified_identity",
            "status": "active", "lifetime": "frame",
            "statement": "The exact selected identity is record-7", "depends_on": [],
        })


def test_memory_dependency_versions_invalidate_claim_and_commitment() -> None:
    journal = WorkerJournal(worker_id="dependency_memory")
    journal.record_memory_updates(
        step=1,
        frame_id="frame:1",
        state=_memory_state(
            {
                "fact_type": "evidence", "key": "bounded_results",
                "status": "active", "lifetime": "attempt",
                "statement": "The bounded result set has one qualifying record",
                "depends_on": [],
            },
            {
                "fact_type": "claim", "key": "candidate_set_complete",
                "status": "active", "lifetime": "attempt",
                "statement": "The qualifying candidate set is complete",
                "depends_on": ["evidence:bounded_results"],
            },
            {
                "fact_type": "commitment", "key": "process_candidate",
                "status": "active", "lifetime": "attempt",
                "statement": "Process the established qualifying record",
                "depends_on": ["claim:candidate_set_complete"],
            },
        ),
    )
    established = build_worker_memory_view(journal, current_frame_id="frame:2")
    assert len(established.established_claims) == 1
    assert len(established.active_commitments) == 1
    rendered = established.render_prompt_section()
    assert 'depends_on=["evidence:bounded_results"]' in rendered
    assert 'depends_on=["claim:candidate_set_complete"]' in rendered
    assert "step:1:memory" not in rendered

    journal.record_memory_updates(
        step=2,
        frame_id="frame:2",
        state=_memory_state({
            "fact_type": "evidence", "key": "bounded_results",
            "status": "active", "lifetime": "attempt",
            "statement": "The result set was corrected and needs reevaluation",
            "depends_on": [],
        }),
    )
    invalidated = build_worker_memory_view(journal, current_frame_id="frame:2")
    assert invalidated.established_claims == ()
    assert invalidated.active_commitments == ()


def test_executing_phase_requires_a_valid_active_commitment_atomically() -> None:
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

    with pytest.raises(ValueError, match="requires an active Commitment"):
        journal.record_memory_updates(step=1, frame_id="frame:1", state=invalid)

    assert journal.events == []


def test_existing_valid_commitment_permits_later_executing_decisions() -> None:
    journal = WorkerJournal(worker_id="execution_phase")
    _record_dependency_chain(
        journal, source_type="evidence", source_key="bounded_results",
        claim_key="selection_established", commitment_key="execute_selection",
    )

    journal.record_memory_updates(
        step=2,
        frame_id="frame:2",
        state=WorkerState(
            status="executing",
            summary="Continue the active commitment",
            memory_updates=[],
        ),
    )

    rendered = build_worker_memory_view(
        journal, current_frame_id="frame:2",
    ).render_prompt_section()
    assert "Execute the active Commitment" in rendered
    assert "from exact dependencies" in rendered
    assert "commitment:execute_selection" in rendered


def test_receipt_keeps_invocation_commitment_refs_after_frame_expiry() -> None:
    journal = WorkerJournal(worker_id="invocation_time")
    _record_dependency_chain(
        journal, source_type="observation", source_key="control_visible",
        claim_key="control_actionable", commitment_key="activate_control",
    )
    commitment = journal.events[-1]
    receipt = journal.record_action_result(
        step=1,
        frame_id="frame:1",
        tool="tap",
        args={"description": "Activate the exact control"},
        result={"status": "executed", "action_type": "tap", "no_effect": True},
        commitment_refs=(commitment.event_ref,),
    )

    assert receipt.receipt is not None
    assert receipt.receipt.commitment_refs == (commitment.event_ref,)
    memory = build_worker_memory_view(journal, current_frame_id="frame:2")
    assert memory.active_commitments == ()
    assert [event.key for event in memory.transition_commitments] == [
        "activate_control"
    ]
    rendered = memory.render_prompt_section()
    assert "Reconcile only the Commitments bound to the latest invocation" in rendered
    assert "Commitments bound to latest invocation" in rendered
    assert "commitment:activate_control" in rendered
    assert "Never rename or repeat it" in rendered
    assert "Execute the active Commitment" not in rendered

    journal.record_memory_updates(
        step=2,
        frame_id="frame:2",
        state=_memory_state({
            "fact_type": "commitment", "key": "activate_control",
            "status": "completed", "lifetime": "attempt",
            "statement": "The exact control was activated",
            "depends_on": [],
        }),
    )
    assert journal.events[-1].depends_on == commitment.depends_on


def test_runtime_answer_must_be_integrated_before_execution() -> None:
    journal = WorkerJournal(worker_id="runtime_answer")
    journal.record_memory_updates(
        step=1,
        frame_id="frame:1",
        state=_memory_state(
            {
                "fact_type": "evidence", "key": "candidate",
                "status": "active", "lifetime": "attempt",
                "statement": "Candidate A is established", "depends_on": [],
            },
            {
                "fact_type": "claim", "key": "candidate_set",
                "status": "active", "lifetime": "attempt",
                "statement": "The candidate set is established",
                "depends_on": ["evidence:candidate"],
            },
            {
                "fact_type": "commitment", "key": "apply_candidate",
                "status": "active", "lifetime": "attempt",
                "statement": "Apply Candidate A to the user-owned destination",
                "depends_on": ["claim:candidate_set"],
            },
        ),
    )
    journal.record_runtime_result(
        step=2,
        result={"_runtime_memory_statement": "The exact destination is Root/team/archive"},
    )

    pending = build_worker_memory_view(journal, current_frame_id="frame:3")
    assert [event.key for event in pending.pending_runtime_evidence] == [
        "user_response_2"
    ]
    assert "Authoritative evidence awaiting integration" in pending.render_prompt_section()
    with pytest.raises(ValueError, match="integrated through Claim"):
        journal.record_memory_updates(
            step=3,
            frame_id="frame:3",
            state=WorkerState(
                status="executing",
                summary="Execute without consuming the answer",
                memory_updates=[],
            ),
        )

    journal.record_memory_updates(
        step=3,
        frame_id="frame:3",
        state=WorkerState.model_validate({
            "status": "executing",
            "summary": "Integrate the authoritative destination before execution",
            "memory_updates": [
                {
                    "fact_type": "claim", "key": "destination",
                    "status": "active", "lifetime": "attempt",
                    "statement": "The exact destination is Root/team/archive",
                    "depends_on": ["evidence:user_response_2"],
                },
                {
                    "fact_type": "commitment", "key": "apply_candidate",
                    "status": "active", "lifetime": "attempt",
                    "statement": "Apply Candidate A to Root/team/archive",
                    "depends_on": ["claim:candidate_set", "claim:destination"],
                },
            ],
        }),
    )
    integrated = build_worker_memory_view(journal, current_frame_id="frame:3")
    assert integrated.pending_runtime_evidence == ()

    journal.record_memory_updates(
        step=4,
        frame_id="frame:4",
        state=_memory_state({
            "fact_type": "commitment", "key": "apply_candidate",
            "status": "completed", "lifetime": "attempt",
            "statement": "Candidate A was applied to Root/team/archive",
            "depends_on": ["claim:candidate_set", "claim:destination"],
        }),
    )
    after_completion = build_worker_memory_view(journal, current_frame_id="frame:4")
    assert after_completion.pending_runtime_evidence == ()


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
        assert "no active dependency" in str(exc)
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


def test_memory_projection_compacts_versions_but_keeps_ordered_journal() -> None:
    journal = WorkerJournal(worker_id="temporal_memory")
    for step in range(1, 13):
        journal.record_memory_updates(
            step=step,
            frame_id=f"frame:{step}",
            state=_memory_state({
                "fact_type": "evidence", "key": "progress",
                "status": "active", "lifetime": "attempt",
                "statement": f"Progress version {step}", "depends_on": [],
            }),
        )

    view = build_worker_memory_view(journal)

    assert len(journal.events) == 12
    assert [event.sequence for event in journal.events] == list(range(1, 13))
    assert [event.statement for event in view.accumulated_evidence] == [
        "Progress version 12"
    ]
    assert len(view.state_timeline) == 8
    assert view.state_timeline[-1].startswith("t=12:")
