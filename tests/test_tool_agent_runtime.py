from __future__ import annotations

import json
from types import SimpleNamespace

from gui_agent.core.tool_agent.contracts import (
    DynamicActionSpec,
    MaterializedFrame,
    WorkerSpec,
)
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

    outcome = runtime._run_worker(spec)

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
