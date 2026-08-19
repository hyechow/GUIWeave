from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from gui_agent.adapters.browser.actions import BrowserAction
from gui_agent.core.tool_agent.contracts import MaterializedFrame, WorkerSpec
from gui_agent.core.tool_agent.data_store import RuntimeDataStore
from gui_agent.core.tool_agent.runtime import ToolAgentRuntime


_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "tool_agent"
    / "task544_result_binding.json"
)


class _RecordingExecutor:
    def __init__(self) -> None:
        self.actions = []

    def execute(self, decision, **_kwargs) -> bool:
        self.actions.append(decision.action)
        return True


def test_task544_replay_injects_transform_result_without_model_transcription(
    monkeypatch,
) -> None:
    case = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    runtime = object.__new__(ToolAgentRuntime)
    runtime.data_store = RuntimeDataStore()
    descriptor = runtime.data_store.put_result(
        case["transform_result"]["value"],
        case["transform_result"]["schema"],
    )
    assert descriptor.ref == "result:1"
    spec = WorkerSpec.model_validate(case["worker_spec"])
    actions = runtime._initial_worker_actions(spec)
    bound = next(item for item in actions if item.name == "enter_computed_description")
    runtime.bundle = SimpleNamespace(
        make_action=lambda payload: BrowserAction.model_validate(payload)
    )
    runtime.perception_mode = "enhanced"
    runtime._executor = _RecordingExecutor()
    runtime._visualizer = None
    runtime.platform = object()
    runtime._trace = lambda *_args, **_kwargs: None
    monkeypatch.setattr(
        "gui_agent.core.tool_agent.runtime.settle_after_action",
        lambda _platform, _png, **_kwargs: (0.0, False),
    )

    payload, terminal = runtime._execute_worker_tool(
        spec,
        actions,
        {
            "name": bound.name,
            "args": {
                "x": 534,
                "y": 194,
            },
        },
        b"recorded-turn-39",
        MaterializedFrame(
            frame_id="recorded-task544-turn39",
            screenshot_path="recorded-task544-turn39.png",
            controls=[case["recorded_editor_state_after_clear"]],
        ),
    )

    executed = runtime._executor.actions[-1]
    assert executed.action_type == "type"
    assert executed.text == "3 customer(s) love it!"
    assert payload["status"] == "executed"
    assert terminal is None
