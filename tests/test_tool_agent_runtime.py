from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from gui_agent.core.tool_agent.contracts import (
    DynamicActionSpec,
    MaterializedFrame,
    WorkerOutcome,
    WorkerSpec,
)
from gui_agent.core.tool_agent.data_store import RuntimeDataStore
from gui_agent.core.tool_agent.runtime import ToolAgentRuntime


def _state(*, missing: bool) -> str:
    return json.dumps(
        {
            "status": "exploring",
            "summary": "A separate apply control is visible.",
            "established_facts": [],
            "open_gaps": ["Apply the configured filter"] if missing else [],
            "coverage": {},
            "action_space_status": "missing_action" if missing else "sufficient",
            "missing_action": "Tap the visible apply control" if missing else "",
            "next_instruction": "Continue the same subgoal on this frame.",
        }
    )


class _Worker:
    def __init__(self) -> None:
        self.responses = [
            SimpleNamespace(
                content=_state(missing=True),
                tool_calls=[
                    {
                        "id": "patch-1",
                        "name": "request_action_patch",
                        "args": {
                            "name": "apply_visible_filter",
                            "capability": "tap",
                            "description": "Apply the currently configured visible filter",
                            "reason": "The current frame shows a separate apply control.",
                        },
                    }
                ],
            ),
            SimpleNamespace(
                content=_state(missing=False),
                tool_calls=[
                    {
                        "id": "tap-1",
                        "name": "apply_visible_filter",
                        "args": {"x": 900, "y": 650},
                    }
                ],
            ),
        ]
        self.bound_tool_names: list[set[str]] = []

    def bind_tools(self, tools, **kwargs):
        del kwargs
        self.bound_tool_names.append({tool["function"]["name"] for tool in tools})
        return self

    def invoke(self, messages):
        del messages
        return self.responses.pop(0)


class _Executor:
    def __init__(self) -> None:
        self.actions = []

    def execute(self, decision, **kwargs):
        del kwargs
        self.actions.append(decision.action)
        return True


class _EmptyContentWorker:
    def __init__(self) -> None:
        self.mode = ""

    def bind_tools(self, tools, **kwargs):
        del tools, kwargs
        self.mode = "action"
        return self

    def bind(self, **kwargs):
        del kwargs
        self.mode = "state"
        return self

    def invoke(self, messages):
        del messages
        if self.mode == "action":
            return SimpleNamespace(
                content="",
                tool_calls=[{
                    "id": "tap-1",
                    "name": "runtime_tap_visible",
                    "args": {"x": 400, "y": 300, "description": "Advance"},
                }],
            )
        return SimpleNamespace(content=_state(missing=False), tool_calls=[])


def test_worker_patches_action_space_and_acts_on_same_frame(monkeypatch) -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.trace = []
    runtime.worker = _Worker()
    runtime._executor = _Executor()
    runtime.platform = object()
    observe_calls = []

    def _observe(spec):
        observe_calls.append(spec)
        return (
            MaterializedFrame(
                frame_id="frame:1",
                screenshot_path="frame.png",
            ),
            b"png",
        )

    runtime._observe = _observe
    monkeypatch.setattr(
        "gui_agent.core.tool_agent.runtime.settle_after_action",
        lambda platform, png, *, action_type: (0.0, False),
    )
    spec = WorkerSpec(
        goal="Complete one cohesive filtered-data subgoal",
        success_criteria=["The configured filter is applied"],
        actions=[
            DynamicActionSpec(
                name="reveal_more",
                capability="scroll",
                description="Reveal more content",
                fixed_args={"direction": "down"},
            )
        ],
        result_schema={"type": "number"},
        max_steps=1,
    )

    outcome = runtime._run_worker("filtered_subgoal", spec)

    assert outcome.steps == 1
    assert len(observe_calls) == 1
    assert "apply_visible_filter" not in runtime.worker.bound_tool_names[0]
    assert "apply_visible_filter" in runtime.worker.bound_tool_names[1]
    assert len(runtime._executor.actions) == 1
    assert runtime._executor.actions[0].action_type == "tap"
    assert runtime._executor.actions[0].x == 900
    assert runtime._executor.actions[0].y == 650
    patches = [event for event in runtime.trace if event["event"] == "worker_action_patch"]
    assert len(patches) == 1
    assert patches[0]["frame_id"] == "frame:1"


def test_worker_recovers_missing_state_content_without_changing_action(monkeypatch) -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.trace = []
    runtime.worker = _EmptyContentWorker()
    runtime._executor = _Executor()
    runtime.platform = object()
    runtime._observe = lambda spec: (
        MaterializedFrame(frame_id="frame:1", screenshot_path="frame.png"),
        b"png",
    )
    monkeypatch.setattr(
        "gui_agent.core.tool_agent.runtime.settle_after_action",
        lambda platform, png, *, action_type: (0.0, False),
    )
    spec = WorkerSpec(
        goal="Advance one cohesive subgoal",
        success_criteria=["The visible control is activated"],
        actions=[DynamicActionSpec(
            name="reveal_more",
            capability="scroll",
            description="Reveal more content",
            fixed_args={"direction": "down"},
        )],
        result_schema={"type": "number"},
        max_steps=1,
    )

    outcome = runtime._run_worker("advance_subgoal", spec)

    assert outcome.phase == "failed"
    assert len(runtime._executor.actions) == 1
    assert runtime._executor.actions[0].x == 400
    recovered = [event for event in runtime.trace if event["event"] == "worker_state_recovered"]
    assert len(recovered) == 1
    assert recovered[0]["tool"] == "runtime_tap_visible"


def test_python_transform_is_blocked_until_collection_coverage_is_complete() -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.data_store = RuntimeDataStore()
    _, collection, _ = runtime.data_store.put_chunk(
        requirement_id="records",
        frame_id="frame:1",
        provider="structured",
        rows=[{"value": "partial"}],
        row_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        coverage={
            "source_scope": "structured_surface",
            "window_key": "page:1",
            "page_index": 1,
            "page_count": 2,
            "has_next_page": True,
            "at_end": False,
            "partial": True,
        },
    )
    transform = DynamicActionSpec(
        name="materialize_records",
        capability="python_transform",
        description="Materialize the collected records",
        fixed_args={"source": "def transform(rows):\n    return rows"},
    )
    spec = WorkerSpec(
        profile="collector",
        goal="Collect all records",
        success_criteria=["Collection coverage is complete"],
        data_requirements=[{
            "id": "records",
            "description": "Collect all records",
            "row_schema": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        }],
        actions=[transform],
        result_schema={
            "type": "array",
            "items": {"type": "object"},
        },
    )

    with pytest.raises(ValueError, match="coverage is 'incomplete'"):
        runtime._execute_worker_tool(
            spec,
            [transform],
            {
                "name": "materialize_records",
                "args": {"data_ref": collection.ref},
            },
            b"png",
        )


def test_runtime_streams_live_logs_and_observation_artifacts(tmp_path) -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.trace = []
    runtime.log_dir = tmp_path
    statuses = []
    runtime._status_cb = statuses.append
    runtime.data_store = RuntimeDataStore()
    runtime.perception_mode = "enhanced"
    runtime._frame_no = 0
    runtime.bundle = object()
    runtime.platform = object()
    runtime.materializer = SimpleNamespace(
        model="observer-model",
        observe=lambda **_kwargs: (
            MaterializedFrame(
                frame_id="frame:1",
                screenshot_path=str(tmp_path / "screenshot_tool_agent_1.png"),
            ),
            b"png",
        ),
    )
    spec = WorkerSpec(
        profile="operator",
        goal="Reach the requested state",
        success_criteria=["The state is reached"],
        actions=[DynamicActionSpec(
            name="advance",
            capability="tap",
            description="Advance the goal",
        )],
        result_schema={"type": "boolean"},
    )

    runtime._observe(spec)

    events = [
        json.loads(line)
        for line in (tmp_path / "tool_agent_events.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    live_trace = json.loads(
        (tmp_path / "tool_agent_trace.json").read_text(encoding="utf-8")
    )
    assert events[0]["layer"] == "observer"
    assert events[0]["event"] == "observe"
    assert "Observe frame:1" in events[0]["message"]
    assert live_trace["phase"] == "running"
    assert live_trace["trace"] == events
    assert "--- Turn 1 ---" in (
        tmp_path / "tool_agent.log"
    ).read_text(encoding="utf-8")
    assert events[0]["timestamp"]
    assert statuses == ["Observer · Observe frame:1 for ?: no collection refs"]
    assert (tmp_path / "observation_tool_agent_1.json").is_file()
    assert (tmp_path / "tool_agent_data_store.json").is_file()


class _CodingMaster:
    def __init__(self, *sources: str) -> None:
        self.sources = list(sources)

    def bind(self, **kwargs):
        del kwargs
        return self

    def invoke(self, messages):
        del messages
        return SimpleNamespace(content=self.sources.pop(0))


class _InterruptingMaster:
    def bind(self, **kwargs):
        del kwargs
        return self

    def invoke(self, messages):
        del messages
        raise KeyboardInterrupt


def _coding_program(terminal: str) -> str:
    return f'''def run(ctx):
    result = ctx.gui_worker(
        worker_id="collect_records",
        goal="Collect the requested records",
        success_criteria=["The requested records are collected"],
        data_requirements=[],
        actions=[{{
            "name": "reveal_more",
            "capability": "scroll",
            "description": "Reveal more records",
            "fixed_args": {{"direction": "down"}},
            "exposed_args": ["amount"],
        }}],
        result_schema={{"type": "array"}},
        max_steps=4,
    )
    {terminal}
'''


def test_runtime_replans_program_without_repeating_completed_gui_worker(tmp_path) -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.max_master_programs = 2
    runtime.max_compile_attempts = 1
    runtime.data_store = RuntimeDataStore()
    runtime.trace = []
    runtime.master = _CodingMaster(
        _coding_program('ctx.replan("Use the completed result")'),
        _coding_program('ctx.finish(result["result_ref"]["ref"])'),
    )
    runtime.master_cfg = SimpleNamespace(model="coding-master")
    runtime.worker_cfg = SimpleNamespace(model="visual-worker")
    runtime.materializer = SimpleNamespace(model="perception")
    runtime.perception_mode = "enhanced"
    runtime.log_dir = tmp_path
    worker_calls = []

    def run_worker(worker_id, spec):
        assert worker_id == "collect_records"
        worker_calls.append(spec)
        descriptor = runtime.data_store.put_result(
            [],
            spec.result_schema,
            summary="Collected once",
        )
        return WorkerOutcome(
            phase="completed",
            summary="Collected once",
            result_ref=descriptor,
            steps=2,
        )

    runtime._run_worker = run_worker

    run = runtime.run("Collect the requested records")

    assert run.phase == "completed"
    assert run.output == []
    assert len(worker_calls) == 1
    assert any(event["event"] == "master_replan" for event in run.trace)
    assert any(event["event"] == "master_worker_reuse" for event in run.trace)
    assert (tmp_path / "tool_agent_trace.json").is_file()


def test_runtime_interruption_is_sealed_as_a_reportable_failed_run(tmp_path) -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.max_master_programs = 1
    runtime.max_compile_attempts = 1
    runtime.data_store = RuntimeDataStore()
    runtime.trace = []
    runtime.master = _InterruptingMaster()
    runtime.master_cfg = SimpleNamespace(model="coding-master")
    runtime.worker_cfg = SimpleNamespace(model="visual-worker")
    runtime.materializer = SimpleNamespace(model="perception")
    runtime.perception_mode = "enhanced"
    runtime.log_dir = tmp_path
    runtime._status_cb = None

    run = runtime.run("Collect the requested records")

    assert run.phase == "failed"
    assert "interrupted" in run.summary
    assert [event["event"] for event in run.trace] == [
        "runtime_started",
        "runtime_interrupted",
        "runtime_finished",
    ]
    persisted = json.loads(
        (tmp_path / "tool_agent_trace.json").read_text(encoding="utf-8")
    )
    assert persisted["phase"] == "failed"
