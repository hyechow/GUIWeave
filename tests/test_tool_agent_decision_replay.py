from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from langchain_core.messages import AIMessage

from gui_agent.core.tool_agent.contracts import WorkerSpec
from replay.decision import (
    replay_master_decision,
    replay_worker_decision,
)


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
    text = (
        "memory\n\n## Current MaterializedFrame (compact semantic projection)\n"
        "instructions\n{\"title\": \"stale\"}"
        if image else "frozen frame"
    )
    human_parts = [{"label": "part_1", "type": "text", "text": text}]
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
    repair_history: bool = False,
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
    }), encoding="utf-8")
    report = _snapshot("tool_agent.worker", image=True)
    if repair_history:
        invalid_call = {
            "name": "continue_with_actions",
            "args": {"state": _state("completed"), "actions": []},
            "id": "recorded-invalid-call",
        }
        report["roles"].extend([
            {"role": "assistant", "parts": [{
                "label": "tool_calls", "type": "text",
                "text": json.dumps([invalid_call]),
            }]},
            {"role": "human", "parts": [{"type": "text", "text": "Protocol repair"}]},
        ])
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
            "context_reports": [report],
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
    assert "recorded static prompt" not in model.calls[0][0].content
    assert "Traverse the visible collection" in model.calls[0][0].content
    assert "stale" not in model.calls[0][1].content[0]["text"]


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


def test_worker_replay_preserves_recorded_protocol_repair_history(tmp_path) -> None:
    run_dir = _worker_run(
        tmp_path,
        recorded_tool="complete",
        recorded_actions=[],
        repair_history=True,
    )
    model = _RecordedModel(
        _tool_call("complete", {"state": _state("completed"), "evidence": ["done"]}),
    )

    result = replay_worker_decision(run_dir, frame=1, llm=model)

    assert result["status"] == "passed"
    assert len(model.calls[0]) == 4
    assert "Protocol repair" in model.calls[0][-1].content


def test_worker_replay_applies_current_singleton_contract(tmp_path) -> None:
    run_dir = _worker_run(
        tmp_path,
        recorded_tool="continue_with_actions",
        recorded_actions=[{"name": "runtime_scroll_visible", "args": {}}],
    )
    spec = WorkerSpec.model_validate({
        "profile": "collector",
        "goal": "Collect the one authoritative value for the exact scope.",
        "success_criteria": ["The authoritative value is visible."],
        "data_requirements": [{
            "id": "answer",
            "description": "One authoritative value.",
            "cardinality": "one",
            "row_schema": {"value": "number"},
        }],
        "actions": [{
            "name": "scroll_content",
            "capability": "scroll",
            "description": "Scroll the visible content.",
            "exposed_args": ["direction", "amount"],
        }],
    })
    observation = json.loads(
        (run_dir / "observation_tool_agent_1.json").read_text(encoding="utf-8")
    )
    observation["collections"] = [{
        "ref": "collection:answer",
        "requirement_id": "answer",
        "chunk_refs": ["chunk:answer:1"],
        "row_count": 1,
        "row_schema": spec.data_requirements[0].row_schema,
        "coverage": {"scope_status": "met", "status": "incomplete"},
    }]
    (run_dir / "observation_tool_agent_1.json").write_text(
        json.dumps(observation), encoding="utf-8",
    )
    model = _RecordedModel(
        _tool_call("complete", {
            "state": _state("completed"),
            "evidence": ["one scope-matched row"],
        }),
    )

    result = replay_worker_decision(
        run_dir,
        frame=1,
        worker_spec=spec,
        expectation={"tool": "complete", "state_status": "completed"},
        llm=model,
    )

    assert result["status"] == "passed"
    assert result["samples"][0]["tool"] == "complete"
    assert "complete" in {
        tool["function"]["name"] for tool in model.bound_tools
    }


def test_master_replay_uses_current_prompt_knowledge_and_structural_expectation(
    monkeypatch, tmp_path,
) -> None:
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
    human = json.dumps({"task": {
        "platform": {},
        "application_knowledge": "old fact",
    }})
    report = _snapshot("tool_agent.master")
    report["roles"][1]["parts"][0]["text"] = human
    (tmp_path / "context.json").write_text(json.dumps({
        "platform": "android",
        "goal": "test",
        "knowledge": {"app_name": "Files"},
    }), encoding="utf-8")
    (tmp_path / "tool_agent_trace.json").write_text(json.dumps({"trace": [{
        "index": 1,
        "event": "master_compile_attempt",
        "attempt": 1,
        "source": source,
        "diagnostics": [],
        "context_reports": [report],
    }]}), encoding="utf-8")
    knowledge = SimpleNamespace(orchestrator_context=lambda _goal: "current fact")
    monkeypatch.setattr(
        "replay.decision.load_knowledge_for_app",
        lambda _name, _platform: knowledge,
    )
    model = _RecordedModel(AIMessage(content=source))

    result = replay_master_decision(
        tmp_path,
        llm=model,
        expectation={
            "reviewed": True,
            "worker_count": 1,
            "worker_profiles": ["operator"],
            "data_cardinalities": [],
            "finish_effect": "mutation",
        },
    )

    assert result["status"] == "passed"
    assert "Coding Master" in model.calls[0][0].content
    assert "recorded static prompt" not in model.calls[0][0].content
    assert "current fact" in model.calls[0][1].content
    assert "old fact" not in model.calls[0][1].content
