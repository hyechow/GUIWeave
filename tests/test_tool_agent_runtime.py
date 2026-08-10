from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from gui_agent.adapters.browser.actions import BrowserAction
from gui_agent.core.tool_agent.contracts import (
    DynamicActionSpec,
    MaterializedFrame,
    WorkerOutcome,
    WorkerSpec,
)
from gui_agent.core.tool_agent.data_store import RuntimeDataStore
from gui_agent.core.tool_agent.runtime import ToolAgentRuntime
from gui_agent.adapters.browser.control_grounding import ground_action_to_nearest_control


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


def test_private_access_context_reaches_worker_but_is_redacted_from_trace() -> None:
    from gui_agent.core.tool_agent.runtime import _access_log_redactions

    access_context = (
        "# Deployment\n"
        "Account `runtime-user-73` / password `runtime-secret-73`"
    )
    runtime = object.__new__(ToolAgentRuntime)
    runtime._worker_access_context = access_context
    runtime._master_knowledge = "Account settings are available from the profile menu."
    runtime._access_log_redactions = _access_log_redactions(access_context)
    runtime.trace = []
    spec = WorkerSpec(
        goal="Reach the authenticated page",
        success_criteria=["The authenticated page is visible"],
        actions=[DynamicActionSpec(
            name="submit_login",
            capability="tap",
            description="Submit the visible login form",
        )],
    )

    prompt = runtime._worker_system_prompt(spec, spec.actions)

    assert "Session access context" in prompt
    assert "runtime-user-73" in prompt
    assert "runtime-secret-73" in prompt
    assert "Application knowledge" in prompt
    assert "profile menu" in prompt

    runtime._trace(
        "worker_decision",
        state={"status": "exploring", "summary": "Using runtime-secret-73"},
        tool="runtime_type_visible",
        args={"text": "runtime-secret-73"},
        context_reports=[{"system_prompt": prompt}],
    )

    rendered_trace = json.dumps(runtime.trace, ensure_ascii=False)
    assert "runtime-user-73" not in rendered_trace
    assert "runtime-secret-73" not in rendered_trace
    assert "session access value redacted" in rendered_trace


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


class _GroundingExecutor(_Executor):
    def ground_coordinates(self, decision, controls):
        return ground_action_to_nearest_control(
            decision,
            controls,
            viewport_size=(1281, 963),
        )


class _Visualizer:
    def __init__(self) -> None:
        self.points = []
        self.clear_calls = 0

    def show_action(self, action) -> None:
        snap = action.snap if isinstance(action.snap, dict) else {}
        point = snap.get("snapped") or [action.x, action.y]
        self.points.append(tuple(point))

    def clear(self) -> None:
        self.clear_calls += 1


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


class _ArrayCoordinateWorker:
    def bind_tools(self, tools, **kwargs):
        del tools, kwargs
        return self

    def invoke(self, messages):
        del messages
        return SimpleNamespace(
            content="",
            tool_calls=[{
                "id": "type-1",
                "name": "runtime_type_visible",
                "args": {
                    "state": {
                        "status": "exploring",
                        "summary": "The date input is visible.",
                        "next_instruction": "Enter the required date.",
                    },
                    "x": [200, 380],
                    "y": [200, 380],
                    "text": "01/01/2023",
                    "description": "Enter the start date",
                },
            }],
        )


class _RepeatedThenGroundedWorker:
    def __init__(self) -> None:
        self.calls = 0

    def bind_tools(self, tools, **kwargs):
        del tools, kwargs
        return self

    def invoke(self, messages):
        del messages
        self.calls += 1
        args = {
            "state": {
                "status": "exploring",
                "summary": "The Purchase Date to field is empty.",
                "next_instruction": "Enter the end date.",
            },
            "x": 207,
            "y": 550,
            "text": "05/31/2023",
            "description": "Enter the end date into the Purchase Date to input",
        }
        if self.calls == 4:
            args["x"] = 207
            args["y"] = 448
        return SimpleNamespace(
            content="",
            tool_calls=[{
                "id": f"type-{self.calls}",
                "name": "runtime_type_visible",
                "args": args,
            }],
        )


class _RepeatedEffectiveScrollWorker:
    def __init__(self) -> None:
        self.calls = 0

    def bind_tools(self, tools, **kwargs):
        del tools, kwargs
        return self

    def invoke(self, messages):
        del messages
        self.calls += 1
        return SimpleNamespace(
            content="",
            tool_calls=[{
                "id": f"scroll-{self.calls}",
                "name": "reveal_more",
                "args": {
                    "state": {
                        "status": "collecting",
                        "summary": "More visual content remains below.",
                        "next_instruction": "Continue scrolling to collect it.",
                    },
                    "amount": "medium",
                },
            }],
        )


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


def test_worker_compatibly_accepts_missing_content_without_another_llm_call(monkeypatch) -> None:
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
        max_steps=1,
    )

    outcome = runtime._run_worker("advance_subgoal", spec)

    assert outcome.phase == "failed"
    assert len(runtime._executor.actions) == 1
    assert runtime._executor.actions[0].x == 400
    recovered = [event for event in runtime.trace if event["event"] == "worker_state_recovered"]
    assert recovered == []
    decisions = [event for event in runtime.trace if event["event"] == "worker_decision"]
    assert decisions[0]["state_source"] == "runtime_compat"
    assert "assistant content state unavailable" in " ".join(
        decisions[0]["state_compatibility"]
    )


def test_retried_gui_worker_retains_bounded_journal_experience(monkeypatch) -> None:
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
        max_steps=1,
    )

    first = runtime._run_worker("advance_subgoal", spec)
    second = runtime._run_worker("advance_subgoal", spec)

    assert first.phase == second.phase == "failed"
    starts = [event for event in runtime.trace if event["event"] == "worker_started"]
    assert [(event["attempt"], event["retained_memory_events"]) for event in starts] == [
        (1, 0),
        (2, 1),
    ]
    decisions = [event for event in runtime.trace if event["event"] == "worker_decision"]
    assert [event["memory_event_count"] for event in decisions] == [0, 2]


def test_worker_normalizes_provider_point_schema_and_executes_type(monkeypatch) -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.trace = []
    runtime.worker = _ArrayCoordinateWorker()
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
        goal="Enter a required value",
        success_criteria=["The value is entered"],
        actions=[DynamicActionSpec(
            name="reveal_more",
            capability="scroll",
            description="Reveal more content",
            fixed_args={"direction": "down"},
        )],
        max_steps=1,
    )

    runtime._run_worker("type_value", spec)

    assert len(runtime._executor.actions) == 1
    action = runtime._executor.actions[0]
    assert action.action_type == "type"
    assert action.x == 200
    assert action.y == 380
    assert action.text == "01/01/2023"
    decision = next(
        event for event in runtime.trace if event["event"] == "worker_decision"
    )
    assert decision["state_source"] == "tool_args"
    assert decision["args"]["x"] == 200
    assert decision["args"]["y"] == 380


def test_worker_fuses_third_repeated_action_and_accepts_same_frame_ref_repair(
    monkeypatch,
) -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.trace = []
    runtime.worker = _RepeatedThenGroundedWorker()
    runtime._executor = _GroundingExecutor()
    runtime._visualizer = _Visualizer()
    runtime.platform = object()
    observed = []

    def observe(spec):
        del spec
        observed.append(True)
        return (
            MaterializedFrame(
                frame_id=f"frame:{len(observed)}",
                screenshot_path="frame.png",
                url="http://example.test/orders",
                title="Orders",
                controls=[{
                    "kind": "text_input",
                    "label": "to",
                    "id": "E1WHE5T",
                    "rect": {"x": 212, "y": 428, "w": 184, "h": 32},
                }],
                requirement_scopes={
                    "orders": {"status": "unmet", "applied_filters": {}},
                },
            ),
            b"png",
        )

    runtime._observe = observe
    monkeypatch.setattr(
        "gui_agent.core.tool_agent.runtime.settle_after_action",
        lambda platform, png, *, action_type: (0.0, False),
    )
    spec = WorkerSpec(
        goal="Set the order end date",
        success_criteria=["The end date is set"],
        actions=[DynamicActionSpec(
            name="reveal_more",
            capability="scroll",
            description="Reveal more content",
            fixed_args={"direction": "down"},
        )],
        max_steps=3,
    )

    outcome = runtime._run_worker("ground_date", spec)

    assert outcome.phase == "failed"
    assert len(observed) == 3
    assert runtime.worker.calls == 4
    assert len(runtime._executor.actions) == 3
    assert (runtime._executor.actions[-1].x, runtime._executor.actions[-1].y) == (
        212,
        428,
    )
    assert runtime._executor.actions[-1].snap["method"] == "control_geometry"
    assert runtime._visualizer.points[-2:] == [
        (212.0, 428.0),
        (212.0, 428.0),
    ]
    blocked = [event for event in runtime.trace if event["event"] == "worker_action_blocked"]
    assert len(blocked) == 1
    assert blocked[0]["prior_attempts"] == 2


def test_worker_does_not_fuse_repeated_scrolls_that_change_visual_frame(
    monkeypatch,
) -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.trace = []
    runtime.worker = _RepeatedEffectiveScrollWorker()
    runtime._executor = _Executor()
    runtime.platform = object()
    observe_calls = []

    def observe(spec):
        del spec
        observe_calls.append(True)
        return (
            MaterializedFrame(
                frame_id=f"frame:{len(observe_calls)}",
                screenshot_path="frame.png",
            ),
            b"png",
        )

    runtime._observe = observe
    monkeypatch.setattr(
        "gui_agent.core.tool_agent.runtime.settle_after_action",
        lambda platform, png, *, action_type: (0.0, False),
    )
    spec = WorkerSpec(
        goal="Collect a long visual surface",
        success_criteria=["All relevant visible records are collected"],
        actions=[DynamicActionSpec(
            name="reveal_more",
            capability="scroll",
            description="Reveal more content",
            fixed_args={"direction": "down"},
            exposed_args=["amount"],
        )],
        max_steps=3,
    )

    outcome = runtime._run_worker("visual_collection", spec)

    assert outcome.phase == "failed"
    assert len(observe_calls) == 3
    assert runtime.worker.calls == 3
    assert len(runtime._executor.actions) == 3
    assert not any(
        event["event"] == "worker_action_blocked"
        for event in runtime.trace
    )


def test_vision_only_execution_does_not_use_enhanced_control_geometry(
    monkeypatch,
) -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.perception_mode = "vision-only"
    runtime._executor = _GroundingExecutor()
    runtime._visualizer = None
    runtime.platform = object()
    runtime._trace = lambda *_args, **_kwargs: None
    monkeypatch.setattr(
        "gui_agent.core.tool_agent.runtime.settle_after_action",
        lambda platform, png, *, action_type: (0.0, False),
    )
    action = DynamicActionSpec(
        name="enter_visible_value",
        capability="type",
        description="Enter the value into the visible input",
        exposed_args=["text", "description"],
    )
    spec = WorkerSpec(
        goal="Enter a visible value",
        success_criteria=["The value is entered"],
        actions=[action],
    )
    frame = MaterializedFrame(
        frame_id="frame:1",
        screenshot_path="frame.png",
        controls=[{
            "kind": "text_input",
            "label": "to",
            "rect": {"x": 212, "y": 428, "w": 184, "h": 32},
        }],
    )

    runtime._execute_worker_tool(
        spec,
        [action],
        {
            "name": "enter_visible_value",
            "args": {
                "x": 207,
                "y": 448,
                "text": "05/31/2023",
                "description": "Enter the end date into the visible to input",
            },
        },
        b"png",
        frame,
    )

    executed = runtime._executor.actions[-1]
    assert (executed.x, executed.y) == (207, 448)
    assert executed.snap is None


@pytest.mark.parametrize(
    ("capability", "args", "action_type"),
    [
        ("open_url", {"url": "https://example.test/reviews"}, "navigate"),
        ("back", {}, "back"),
        ("clear_text", {}, "clear_text"),
        ("press_enter", {}, "press_enter"),
    ],
)
def test_runtime_executes_nonspatial_browser_capabilities_through_adapter_action(
    monkeypatch,
    capability: str,
    args: dict[str, str],
    action_type: str,
) -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.bundle = SimpleNamespace(
        make_action=lambda payload: BrowserAction.model_validate(payload)
    )
    runtime.perception_mode = "enhanced"
    runtime._executor = _Executor()
    runtime._visualizer = None
    runtime.platform = object()
    runtime._trace = lambda *_args, **_kwargs: None
    monkeypatch.setattr(
        "gui_agent.core.tool_agent.runtime.settle_after_action",
        lambda platform, png, *, action_type: (0.0, False),
    )
    action = DynamicActionSpec(
        name=f"do_{capability}",
        capability=capability,
        description=f"Execute {capability} for the current subgoal",
        exposed_args=list(args),
    )
    spec = WorkerSpec(
        goal="Advance the browser subgoal",
        success_criteria=["The browser state advances"],
        actions=[action],
    )

    payload, terminal = runtime._execute_worker_tool(
        spec,
        [action],
        {"name": action.name, "args": args},
        b"png",
        MaterializedFrame(frame_id="frame:1", screenshot_path="frame.png"),
    )

    executed = runtime._executor.actions[-1]
    assert isinstance(executed, BrowserAction)
    assert executed.action_type == action_type
    assert getattr(executed, "url", None) == args.get("url")
    assert payload["status"] == "executed"
    assert terminal is None


def test_collector_completion_is_unavailable_until_collection_is_ready() -> None:
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
        actions=[DynamicActionSpec(
            name="reveal_more",
            capability="scroll",
            description="Reveal more collected records",
            fixed_args={"direction": "down"},
        )],
    )

    frame = MaterializedFrame(
        frame_id="frame:1",
        screenshot_path="frame.png",
        collections=[collection],
        requirement_scopes={"records": {"status": "met"}},
    )
    tools = runtime._worker_tools_for_frame(spec, spec.actions, frame)
    assert "complete" not in {tool["function"]["name"] for tool in tools}

    with pytest.raises(ValueError, match="complete is unavailable"):
        runtime._execute_worker_tool(
            spec,
            spec.actions,
            {
                "name": "complete",
                "args": {},
            },
            b"png",
            frame,
        )


def test_ready_collector_completion_uses_runtime_bound_collection_ref() -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.data_store = RuntimeDataStore()
    _, collection, _ = runtime.data_store.put_chunk(
        requirement_id="records",
        frame_id="frame:1",
        provider="structured",
        rows=[{"value": "ready"}],
        row_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
        coverage={
            "scope_status": "met",
            "source_scope": "structured_surface",
            "total_records": 1,
            "page_index": 1,
            "page_count": 1,
            "has_next_page": False,
            "at_end": True,
        },
    )
    spec = WorkerSpec(
        profile="collector",
        goal="Collect all records",
        success_criteria=["Collection coverage is complete"],
        data_requirements=[{
            "id": "records",
            "description": "Collect all records",
            "row_schema": collection.row_schema,
        }],
        actions=[DynamicActionSpec(
            name="reveal_more",
            capability="scroll",
            description="Reveal more collected records",
            fixed_args={"direction": "down"},
        )],
    )
    frame = MaterializedFrame(
        frame_id="frame:1",
        screenshot_path="frame.png",
        collections=[collection],
        requirement_scopes={"records": {"status": "met"}},
    )

    tools = runtime._worker_tools_for_frame(spec, spec.actions, frame)
    complete = next(tool for tool in tools if tool["function"]["name"] == "complete")
    assert "collection_ref" not in complete["function"]["parameters"]["properties"]
    payload, terminal = runtime._execute_worker_tool(
        spec,
        spec.actions,
        {"name": "complete", "args": {"evidence": ["coverage complete"]}},
        b"png",
        frame,
    )

    assert terminal == "complete"
    assert payload["ref"] == collection.ref


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


def _coding_program() -> str:
    return '''def run(ctx):
    result = ctx.gui_worker(
        worker_id="collect_records",
        profile="collector",
        goal="Collect the requested records",
        success_criteria=["The requested records are collected"],
        data_requirements=[{
            "id": "records",
            "description": "Requested records",
            "row_schema": {
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        }],
        actions=[{
            "name": "reveal_more",
            "capability": "scroll",
            "description": "Reveal more records",
            "fixed_args": {"direction": "down"},
            "exposed_args": ["amount"],
        }],
        max_steps=4,
    )
    if result["phase"] != "completed":
        ctx.fail(result["summary"])
    computed = ctx.transform(
        transform_id="count_records",
        inputs=[result["collection_ref"]["ref"]],
        source="def transform(inputs):\\n    return len(inputs[0])",
        result_schema={"type": "integer"},
    )
    ctx.finish(computed["ref"])
'''


def test_runtime_replans_inside_worker_call_without_replaying_program(tmp_path) -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.max_subgoal_replans = 1
    runtime.max_compile_attempts = 1
    runtime.data_store = RuntimeDataStore()
    runtime.trace = []
    replacement = {
        "profile": "collector",
        "goal": "Collect the requested records",
        "success_criteria": ["The requested records are collected"],
        "data_requirements": [{
            "id": "records",
            "description": "Requested records",
            "row_schema": {
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        }],
        "actions": [{
            "name": "reveal_more_aggressively",
            "capability": "scroll",
            "description": "Reveal a larger window of records",
            "fixed_args": {
                "direction": "down",
                "amount": "large",
                "target_area": "main_content",
            },
        }],
        "max_steps": 4,
    }
    runtime.master = _CodingMaster(
        _coding_program(),
        json.dumps({"worker_spec": replacement}),
    )
    runtime.master_cfg = SimpleNamespace(model="coding-master")
    runtime.worker_cfg = SimpleNamespace(model="visual-worker")
    runtime.materializer = SimpleNamespace(model="perception")
    runtime.perception_mode = "enhanced"
    runtime.log_dir = tmp_path
    worker_calls = []

    def run_worker(worker_id, spec):
        worker_calls.append((worker_id, spec))
        if len(worker_calls) == 1:
            return WorkerOutcome(
                phase="failed",
                summary="Need another GUI attempt",
                steps=4,
            )
        _, descriptor, _ = runtime.data_store.put_chunk(
            requirement_id="records",
            frame_id="frame:2",
            provider="structured",
            rows=[{"value": 1}],
            row_schema=spec.data_requirements[0].row_schema,
            coverage={
                "source_scope": "structured_surface",
                "scope_status": "met",
                "traversal_type": "static",
                "partial": False,
                "total_records": 1,
            },
        )
        return WorkerOutcome(
            phase="completed",
            summary="Collected after local replan",
            collection_ref=descriptor,
            steps=2,
        )

    runtime._run_worker = run_worker

    run = runtime.run("Collect the requested records")

    assert run.phase == "completed"
    assert run.output == 1
    assert len(worker_calls) == 2
    assert [worker_id for worker_id, _ in worker_calls] == [
        "collect_records",
        "collect_records_replan_1",
    ]
    assert runtime.master.sources == []
    assert any(event["event"] == "master_worker_redelegated" for event in run.trace)
    assert not any(event["event"] == "subgoal_replan" for event in run.trace)
    assert (tmp_path / "tool_agent_trace.json").is_file()
    replay = json.loads(
        (tmp_path / "tool_agent_replay.json").read_text(encoding="utf-8")
    )
    assert replay["status"] == "passed"
    assert replay["program_count"] == 1
    assert replay["gui_worker_count"] == 1
    assert replay["uses_browser"] is False
    assert replay["uses_llm"] is False


def test_runtime_does_not_replay_frozen_program_after_local_budget_failure(
    tmp_path,
) -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.max_subgoal_replans = 2
    runtime.max_compile_attempts = 1
    runtime.data_store = RuntimeDataStore()
    runtime.trace = []
    runtime.master = _CodingMaster(_coding_program())
    runtime.master_cfg = SimpleNamespace(model="coding-master")
    runtime.worker_cfg = SimpleNamespace(model="visual-worker")
    runtime.materializer = SimpleNamespace(model="perception")
    runtime.perception_mode = "enhanced"
    runtime.log_dir = tmp_path
    worker_calls = []

    def fail_worker(worker_id, spec):
        worker_calls.append((worker_id, spec))
        return WorkerOutcome(
            phase="failed",
            summary="No remaining local strategy",
            steps=4,
        )

    runtime._run_worker = fail_worker

    run = runtime.run("Collect the requested records")

    assert run.phase == "failed"
    assert len(worker_calls) == 1
    assert sum(
        event["event"] == "master_program_execution_started"
        for event in run.trace
    ) == 1
    replay = json.loads(
        (tmp_path / "tool_agent_replay.json").read_text(encoding="utf-8")
    )
    assert replay["status"] == "passed"
    assert replay["program_count"] == 1


def test_runtime_interruption_is_sealed_as_a_reportable_failed_run(tmp_path) -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.max_subgoal_replans = 0
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
    runtime._visualizer = _Visualizer()

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
    replay = json.loads(
        (tmp_path / "tool_agent_replay.json").read_text(encoding="utf-8")
    )
    assert replay["status"] == "unavailable"
    assert runtime._visualizer.clear_calls == 1
