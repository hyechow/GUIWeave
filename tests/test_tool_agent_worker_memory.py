from __future__ import annotations

from types import SimpleNamespace

from gui_agent.core.tool_agent.contracts import (
    DynamicActionSpec,
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
        established_facts=[],
        open_gaps=[f"Complete subgoal {step}"],
        coverage={},
        next_instruction=f"Take action {step + 1}",
    )


def test_worker_memory_is_a_bounded_projection_of_append_only_runtime_facts() -> None:
    journal = WorkerJournal(worker_id="bounded_worker")
    for step in range(1, 13):
        journal.record_turn(
            step=step,
            frame_id=f"frame:{step}",
            state=_state(step),
            tool="runtime_tap_visible",
            args={"x": step, "y": step},
            result={"status": "executed", "no_effect": step % 2 == 0},
        )

    memory = build_worker_memory_view(journal)

    assert [item.event_ref for item in memory.durable_facts] == ["step:12"]
    assert [item.event_ref for item in memory.recent_steps] == [
        "step:9",
        "step:10",
        "step:11",
        "step:12",
    ]
    assert len(memory.compressed_history) == 6
    assert memory.compressed_history[0].startswith("[step:3]")
    assert memory.compressed_history[-1].startswith("[step:8]")
    assert memory.pending_subgoal == "Complete subgoal 12"
    rendered = memory.render_prompt_section()
    assert "runtime reported no_effect" in rendered
    assert "[step:1]" not in rendered
    assert "Only runtime results below are facts" in rendered


def test_worker_context_uses_semantic_frame_variant_before_exceeding_budget() -> None:
    journal = WorkerJournal(worker_id="collector")
    frame = MaterializedFrame.model_validate({
        "frame_id": "frame:1",
        "screenshot_path": "frame.png",
        "controls": [{"kind": "button", "label": "Apply"}],
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
        memory=build_worker_memory_view(journal),
        frame=frame,
        max_chars=4_000,
    )

    frame_decision = next(
        item for item in projection.report["blocks"]
        if item["id"] == "tool_agent.worker.current_frame"
    )
    assert frame_decision["action"] == "compressed"
    assert frame_decision["strategy"] == "drop_descriptor_schemas"
    assert projection.report["after_chars"] < projection.report["before_chars"]
    assert "collection:records" in projection.text
    assert "x" * 100 not in projection.text


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
    assert "before collecting or completing" in projection.text


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
                        "next_instruction": f"Take action {self.step + 1}",
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
    runtime._observe = lambda spec: (
        MaterializedFrame(
            frame_id=f"frame:{next(frames)}",
            screenshot_path="frame.png",
        ),
        b"png",
    )
    monkeypatch.setattr(
        "gui_agent.core.tool_agent.runtime.settle_after_action",
        lambda platform, png, *, action_type: (0.0, False),
    )
    spec = WorkerSpec(
        goal="Advance through three visual states",
        success_criteria=["The final state is visible"],
        actions=[DynamicActionSpec(
            name="reveal_more",
            capability="scroll",
            description="Reveal more content",
            fixed_args={"direction": "down"},
        )],
        max_steps=3,
    )

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
    assert [event["state_source"] for event in decisions] == ["tool_args"] * 3
    assert all(
        kwargs["tool_choice"] == "required"
        and kwargs["extra_body"] == {"enable_thinking": False}
        for kwargs in runtime.worker.bind_kwargs
    )
    assert all(event["context_chars"] > 0 for event in decisions)
    human_log = runtime._human_line(decisions[-1])
    assert "Context    : rebuilt for frame" in human_log
    assert "2 journal events" in human_log
    assert "Protocol   : state=tool_args" in human_log
    assert all(
        any(
            report.get("kind") == "context_compression"
            for report in event["context_reports"]
        )
        for event in decisions
    )
