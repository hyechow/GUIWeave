from __future__ import annotations

import json
from pathlib import Path

from langchain_core.messages import AIMessage

from gui_agent.core.tool_agent.contracts import WorkerSpec
from replay.decision import replay_master_decision, replay_worker_decision


class _RecordedModel:
    def __init__(self, *responses: AIMessage) -> None:
        self.responses = list(responses)
        self.calls: list[list] = []
        self.bound_tools = []

    def bind(self, **_kwargs):
        return self

    def bind_tools(self, tools, **_kwargs):
        self.bound_tools = tools
        return self

    def invoke(self, messages):
        self.calls.append(messages)
        return self.responses.pop(0)


def _snapshot(label: str, *, image: bool = False) -> dict:
    human_parts = [{"label": "part_1", "type": "text", "text": "frozen frame"}]
    if image:
        human_parts.append({"label": "screenshot", "type": "image", "text": "omitted"})
    return {
        "kind": "prompt_snapshot",
        "label": label,
        "roles": [
            {
                "role": "system",
                "parts": [{
                    "label": "part_1",
                    "type": "text",
                    "text": "recorded static prompt\n\n## Worker attempt contract\n{}",
                }],
            },
            {"role": "human", "parts": human_parts},
        ],
    }


def _state(status: str = "exploring") -> dict:
    return {
        "status": status,
        "summary": "Replay decision",
        "established_facts": [],
        "next_instruction": "Continue the recorded subgoal",
    }


def _tool_call(name: str, args: dict) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": f"call-{name}"}],
    )


def _worker_run(
    tmp_path: Path,
    *,
    recorded_tool: str,
    recorded_actions: list[dict],
    controls: list[dict] | None = None,
) -> Path:
    spec = WorkerSpec.model_validate({
        "profile": "operator",
        "goal": "Traverse the visible collection.",
        "success_criteria": ["The collection boundary is reached."],
        "actions": [{
            "name": "scroll_content",
            "capability": "scroll",
            "description": "Scroll the visible collection.",
            "exposed_args": ["direction", "amount"],
        }],
    })
    (tmp_path / "context.json").write_text(
        json.dumps({"platform": "android"}),
        encoding="utf-8",
    )
    (tmp_path / "tool_agent_data_store.json").write_text(
        json.dumps({"values": {}}),
        encoding="utf-8",
    )
    (tmp_path / "screenshot_tool_agent_1.png").write_bytes(b"png")
    (tmp_path / "observation_tool_agent_1.json").write_text(json.dumps({
        "frame_id": "frame:1",
        "screenshot_path": str(tmp_path / "screenshot_tool_agent_1.png"),
        "controls": controls or [],
    }), encoding="utf-8")
    (tmp_path / "tool_agent_trace.json").write_text(json.dumps({"trace": [
        {"index": 1, "event": "runtime_started", "multi_action": True},
        {
            "index": 2,
            "event": "master_worker_dispatch",
            "worker_id": "worker",
            "spec": spec.model_dump(mode="json"),
        },
        {
            "index": 3,
            "event": "worker_decision",
            "worker_id": "worker",
            "frame_id": "frame:1",
            "step": 1,
            "tool": recorded_tool,
            "args": {"actions": recorded_actions},
            "context_reports": [_snapshot("tool_agent.worker", image=True)],
        },
    ]}), encoding="utf-8")
    return tmp_path


def test_worker_replay_compares_equivalent_actions_by_capability(tmp_path) -> None:
    run_dir = _worker_run(
        tmp_path,
        recorded_tool="continue_with_actions",
        recorded_actions=[{"name": "runtime_scroll_visible", "args": {}}],
    )
    model = _RecordedModel(_tool_call("continue_with_actions", {
        "state": _state(),
        "actions": [{
            "name": "scroll_content",
            "args": {
                "direction": "down",
                "amount": "medium",
                "description": "Scroll the visible collection downward",
            },
        }],
    }))

    result = replay_worker_decision(run_dir, frame=1, llm=model)

    assert result["status"] == "passed"
    assert result["expectation"] == {
        "tool": "continue_with_actions",
        "action_capabilities": ["scroll"],
    }
    assert result["samples"][0]["actions"] == ["scroll_content"]
    assert result["samples"][0]["action_capabilities"] == ["scroll"]
    assert {tool["function"]["name"] for tool in model.bound_tools} >= {
        "continue_with_actions",
        "complete",
        "fail",
    }


def test_worker_replay_reports_grounded_visible_action_target(tmp_path) -> None:
    run_dir = _worker_run(
        tmp_path,
        recorded_tool="continue_with_actions",
        recorded_actions=[{"name": "runtime_tap_visible", "args": {"x": 180, "y": 246}}],
        controls=[{
            "kind": "button",
            "label": "Back",
            "value": "Back",
            "rect": {"x": 180, "y": 246, "w": 100, "h": 46},
        }],
    )
    model = _RecordedModel(_tool_call("continue_with_actions", {
        "state": _state(),
        "actions": [{
            "name": "runtime_tap_visible",
            "args": {
                "x": 180,
                "y": 246,
                "description": "Activate the visible Back button",
            },
        }],
    }))

    result = replay_worker_decision(
        run_dir,
        frame=1,
        llm=model,
        expectation={
            "tool": "continue_with_actions",
            "action_capabilities": ["tap"],
            "action_targets": ["Back"],
        },
    )

    assert result["status"] == "passed"
    assert result["samples"][0]["action_targets"] == ["Back"]


def test_worker_replay_applies_one_same_frame_protocol_repair(tmp_path) -> None:
    run_dir = _worker_run(
        tmp_path,
        recorded_tool="complete",
        recorded_actions=[],
    )
    model = _RecordedModel(
        _tool_call("continue_with_actions", {
            "state": _state("completed"),
            "actions": [{"name": "complete", "args": {}}],
        }),
        _tool_call("complete", {"state": _state("completed"), "evidence": ["done"]}),
    )

    result = replay_worker_decision(run_dir, frame="frame:1", llm=model)

    sample = result["samples"][0]
    assert result["status"] == "passed"
    assert sample["tool"] == "complete"
    assert sample["protocol_repairs"] == 1
    assert len(model.calls) == 2
    assert "Protocol repair" in model.calls[1][-1].content


def test_master_replay_uses_current_prompt_and_structural_expectation(tmp_path) -> None:
    source = """def run(ctx):
    outcome = ctx.gui_worker(
        worker_id="operate",
        profile="operator",
        goal="Apply the target state.",
        success_criteria=["Target state is visible."],
        data_requirements=[],
        actions=[{"name": "open_target", "capability": "tap", "description": "Open target"}],
    )
    if outcome["phase"] != "completed":
        ctx.fail("worker failed")
    result = ctx.transform(
        transform_id="confirm",
        inputs=[],
        source="def transform(inputs):\\n    return True",
        result_schema={"type": "boolean"},
    )
    ctx.finish(result["ref"], effect="mutation")
"""
    human = json.dumps({"task": {"platform": {}}})
    report = _snapshot("tool_agent.master")
    report["roles"][1]["parts"][0]["text"] = human
    (tmp_path / "context.json").write_text("{}", encoding="utf-8")
    (tmp_path / "tool_agent_trace.json").write_text(json.dumps({"trace": [{
        "index": 1,
        "event": "master_compile_attempt",
        "attempt": 1,
        "source": source,
        "diagnostics": [],
        "context_reports": [report],
    }]}), encoding="utf-8")
    model = _RecordedModel(AIMessage(content=source))

    result = replay_master_decision(
        tmp_path,
        llm=model,
        expectation={
            "reviewed": True,
            "worker_count": 1,
            "worker_profiles": ["operator"],
            "finish_effect": "mutation",
        },
    )

    assert result["status"] == "passed"
    assert "Coding Master" in model.calls[0][0].content
    assert "recorded static prompt" not in model.calls[0][0].content
