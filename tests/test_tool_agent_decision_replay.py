from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from langchain_core.messages import AIMessage

from gui_agent.core.tool_agent.contracts import DynamicActionSpec, WorkerSpec
from llm.provider_config import ChatProviderConfig
import replay.decision as decision_module
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
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


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
        "memory_updates": [],
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
    actions = [DynamicActionSpec(
        name="scroll",
        capability="scroll",
        description="recorded static action description",
        exposed_args=["direction", "amount"],
    )]
    spec = WorkerSpec.model_validate({
        "profile": "operator",
        "goal": "Traverse the visible collection.",
        "success_criteria": ["The collection boundary is reached."],
        "strategy": {"approach": "Traverse the visible collection."},
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
            "replay_context": {
                "version": 2,
                "worker_spec": spec.model_dump(mode="json"),
                "actions": [
                    action.model_dump(mode="json")
                    for action in actions
                ],
                "executed_tools": [],
                "enhanced": False,
                "multi_action": True,
            },
        },
    ]}), encoding="utf-8")
    return tmp_path


def test_worker_replay_compares_equivalent_actions_by_capability(tmp_path) -> None:
    run_dir = _worker_run(
        tmp_path,
        recorded_tool="continue_with_actions",
        recorded_actions=[{"name": "scroll", "args": {}}],
    )
    model = _RecordedModel(_tool_call("continue_with_actions", {
        "state": _state(),
        "actions": [{
            "name": "scroll",
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
        "action_semantics": [{"capability": "scroll"}],
    }
    assert result["samples"][0]["actions"] == ["scroll"]
    assert result["samples"][0]["action_capabilities"] == ["scroll"]
    assert result["samples"][0]["action_semantics"] == [{
        "capability": "scroll",
        "direction": "down",
        "amount": "medium",
    }]
    assert {tool["function"]["name"] for tool in model.bound_tools} >= {
        "continue_with_actions",
        "complete",
        "report_blocked",
    }
    assert "recorded static prompt" not in model.calls[0][0].content
    assert "Execute the binding `approach`" in model.calls[0][0].content
    assert "A visible ancestor never establishes an unshown descendant" in (
        model.calls[0][0].content
    )
    assert "## Worker attempt contract" not in model.calls[0][0].content
    turn_context = model.calls[0][1].content[0]["text"]
    assert "stale" in turn_context
    assert turn_context.index("Current Worker attempt") < turn_context.index(
        "Current MaterializedFrame"
    )
    assert turn_context.index('"approach"') < turn_context.index('"goal"')
    assert '"approach": "Traverse the visible collection."' in turn_context
    envelope = next(
        tool for tool in model.bound_tools
        if tool["function"]["name"] == "continue_with_actions"
    )
    variants = envelope["function"]["parameters"]["properties"]["actions"]["items"]["oneOf"]
    scroll_variant = next(
        item for item in variants
        if item["properties"]["name"]["const"] == "scroll"
    )
    assert "Never use this on a relevance-ordered web/search result page" in (
        scroll_variant["description"]
    )
    assert "recorded static action description" not in scroll_variant["description"]


def test_worker_replay_withholds_complete_for_visible_commit(tmp_path) -> None:
    run_dir = _worker_run(
        tmp_path,
        recorded_tool="continue_with_actions",
        recorded_actions=[{"name": "scroll", "args": {}}],
    )
    model = _RecordedModel(_tool_call("continue_with_actions", {
        "state": _state(),
        "actions": [{"name": "scroll", "args": {
            "direction": "down",
            "description": "Activate the pending commit",
        }}],
    }))

    replay_worker_decision(
        run_dir, frame=1, visible_commit_control="Apply", llm=model,
    )

    names = {tool["function"]["name"] for tool in model.bound_tools}
    assert "complete" not in names
    assert "completion_requires_recheck" in model.calls[0][1].content[0]["text"]


def test_worker_replay_detects_wrong_launch_app_argument(tmp_path) -> None:
    run_dir = _worker_run(
        tmp_path,
        recorded_tool="continue_with_actions",
        recorded_actions=[{
            "name": "launch_app",
            "args": {"app": "Taodian"},
        }],
    )
    trace_path = run_dir / "tool_agent_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    trace["trace"][-1]["replay_context"]["actions"] = [
        DynamicActionSpec(
            name="launch_app",
            capability="launch_app",
            description="Launch one installed application",
            exposed_args=["app"],
        ).model_dump(mode="json")
    ]
    trace_path.write_text(json.dumps(trace), encoding="utf-8")
    model = _RecordedModel(_tool_call("continue_with_actions", {
        "state": _state(),
        "actions": [{"name": "launch_app", "args": {"app": "Messages"}}],
    }))

    result = replay_worker_decision(run_dir, frame=1, llm=model)

    assert result["status"] == "failed"
    assert result["expectation"]["action_semantics"] == [{
        "capability": "launch_app",
        "app": "Taodian",
    }]
    assert result["samples"][0]["action_semantics"] == [{
        "capability": "launch_app",
        "app": "Messages",
    }]
    assert any(".app" in error for error in result["samples"][0]["errors"])


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


def test_worker_replay_supports_protocol_failed_frame_with_expectation(tmp_path) -> None:
    run_dir = _worker_run(
        tmp_path,
        recorded_tool="continue_with_actions",
        recorded_actions=[{"name": "scroll", "args": {}}],
    )
    trace_path = run_dir / "tool_agent_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    recorded = trace["trace"][-1]
    prior = {**recorded, "index": 3, "step": 0, "frame_id": "frame:0"}
    failure = {
        "index": 4,
        "event": "worker_protocol_error",
        "worker_id": "worker",
        "step": 1,
        "attempt": 1,
        "context_reports": recorded["context_reports"],
    }
    trace["trace"] = [*trace["trace"][:-1], prior, failure]
    trace_path.write_text(json.dumps(trace), encoding="utf-8")
    model = _RecordedModel(_tool_call("continue_with_actions", {
        "state": _state(),
        "actions": [{"name": "scroll", "args": {
            "direction": "down",
            "description": "Scroll the visible collection downward",
        }}],
    }))

    result = replay_worker_decision(
        run_dir,
        frame=1,
        expectation={
            "tool": "continue_with_actions",
            "action_capabilities": ["scroll"],
        },
        llm=model,
    )

    assert result["status"] == "passed"
    assert result["samples"][0]["action_capabilities"] == ["scroll"]


def test_worker_replay_repairs_surface_change_before_batch_suffix(tmp_path) -> None:
    run_dir = _worker_run(
        tmp_path,
        recorded_tool="continue_with_actions",
        recorded_actions=[{"name": "launch_app", "args": {"app": "Messages"}}],
    )
    trace = json.loads((run_dir / "tool_agent_trace.json").read_text())
    trace["trace"][-1]["replay_context"]["actions"] = [
        {"name": "home", "capability": "home", "description": "Go home"},
        {
            "name": "launch_app",
            "capability": "launch_app",
            "description": "Open Messages",
            "exposed_args": ["app"],
        },
    ]
    (run_dir / "tool_agent_trace.json").write_text(json.dumps(trace))
    model = _RecordedModel(
        _tool_call("continue_with_actions", {
            "state": _state(),
            "actions": [
                {"name": "home", "args": {}},
                {"name": "launch_app", "args": {"app": "Messages"}},
            ],
        }),
        _tool_call("continue_with_actions", {
            "state": _state(),
            "actions": [{"name": "launch_app", "args": {"app": "Messages"}}],
        }),
    )

    result = replay_worker_decision(run_dir, frame=1, llm=model)

    sample = result["samples"][0]
    assert sample["action_capabilities"] == ["launch_app"]
    assert sample["protocol_repairs"] == 1


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


def test_worker_replay_uses_current_application_knowledge(
    tmp_path, monkeypatch,
) -> None:
    run_dir = _worker_run(
        tmp_path,
        recorded_tool="continue_with_actions",
        recorded_actions=[{"name": "scroll", "args": {}}],
    )
    context_path = run_dir / "context.json"
    context_path.write_text(json.dumps({
        "platform": "android",
        "knowledge": {"app_name": "Example"},
    }), encoding="utf-8")
    trace_path = run_dir / "tool_agent_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    report = trace["trace"][-1]["context_reports"][0]
    report["roles"][0]["parts"][0]["text"] = (
        "recorded static prompt\n\n## Application knowledge\nold fact"
        "\n\n## Worker attempt contract\n{}"
    )
    trace_path.write_text(json.dumps(trace), encoding="utf-8")
    knowledge = SimpleNamespace(worker_context=lambda: "current fact")
    monkeypatch.setattr(
        "replay.decision.load_knowledge_for_app",
        lambda _name, _platform: knowledge,
    )
    model = _RecordedModel(_tool_call("continue_with_actions", {
        "state": _state(),
        "actions": [{"name": "scroll", "args": {
            "direction": "down",
            "description": "Scroll the visible collection downward",
        }}],
    }))

    result = replay_worker_decision(run_dir, frame=1, llm=model)

    assert result["status"] == "passed"
    assert "current fact" in model.calls[0][0].content
    assert "old fact" not in model.calls[0][0].content


def test_worker_replay_preserves_recorded_singleton_contract(tmp_path) -> None:
    run_dir = _worker_run(
        tmp_path,
        recorded_tool="continue_with_actions",
        recorded_actions=[{"name": "scroll", "args": {}}],
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
        "strategy": {"approach": "Traverse the one authoritative value."},
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
        "coverage": {
            "scope_status": "met", "status": "complete", "cardinality": "one",
        },
    }]
    (run_dir / "observation_tool_agent_1.json").write_text(
        json.dumps(observation), encoding="utf-8",
    )
    trace_path = run_dir / "tool_agent_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    trace["trace"][-1]["replay_context"]["worker_spec"] = spec.model_dump(mode="json")
    trace_path.write_text(json.dumps(trace), encoding="utf-8")
    model = _RecordedModel(
        _tool_call("complete", {
            "state": _state("completed"),
            "evidence": ["one scope-matched row"],
        }),
    )

    result = replay_worker_decision(
        run_dir,
        frame=1,
        expectation={"tool": "complete", "state_status": "completed"},
        llm=model,
    )

    assert result["status"] == "passed"
    assert result["samples"][0]["tool"] == "complete"
    assert "complete" in {
        tool["function"]["name"] for tool in model.bound_tools
    }


def test_worker_replay_marks_redacted_value_action_unavailable(tmp_path) -> None:
    run_dir = _worker_run(
        tmp_path,
        recorded_tool="continue_with_actions",
        recorded_actions=[{
            "name": "runtime_type_visible",
            "args": {"text": "[session access value redacted]"},
        }],
    )
    model = _RecordedModel()

    result = replay_worker_decision(run_dir, frame=1, llm=model)

    assert result["status"] == "unavailable"
    assert result["uses_llm"] is False
    assert model.calls == []


def test_worker_replay_supports_plain_json_action_protocol(tmp_path, monkeypatch) -> None:
    run_dir = _worker_run(
        tmp_path,
        recorded_tool="continue_with_actions",
        recorded_actions=[
            {"name": "scroll", "args": {}},
        ],
    )
    model = _RecordedModel(
        AIMessage(content="{}"),
        AIMessage(content=json.dumps({
            "tool": "continue_with_actions",
            "args": {
                "state": _state(),
                "actions": [{
                    "name": "scroll",
                    "args": {
                        "direction": "down",
                        "amount": "medium",
                        "description": "Scroll the visible collection downward",
                    },
                }],
            },
        })),
    )
    config = ChatProviderConfig(
        provider="standard",
        model="gpt-5.6-luna",
        api_key="test",
        base_url="http://standard.example/v1",
        temperature=None,
        reasoning_effort="low",
        action_protocol="json",
    )
    monkeypatch.setattr(decision_module, "_model", lambda _name: (model, config))

    result = replay_worker_decision(run_dir, frame=1)

    assert result["status"] == "passed"
    assert result["expectation"]["action_capabilities"] == ["scroll"]
    assert result["samples"][0]["actions"] == ["scroll"]
    assert result["samples"][0]["protocol_repairs"] == 1
    assert model.bound_tools == []
    assert any("Decision transport" in message.content for message in model.calls[0])
    assert any('"maxItems":5' in message.content for message in model.calls[0])
    assert "JSON object with only tool and args" in model.calls[1][-1].content


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
        approach="visible target interface",
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
        "platform": {"name": "android", "action_contracts": {"tap": {}}},
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
    model = _RecordedModel(
        AIMessage(content=source),
        AIMessage(content='{"issues": [], "target_bindings": []}'),
    )

    result = replay_master_decision(
        tmp_path,
        llm=model,
        expectation={
            "reviewed": True,
            "compile_attempts": 1,
            "worker_count": 1,
            "worker_profiles": ["operator"],
            "data_cardinalities": [],
            "finish_effect": "mutation",
        },
    )

    assert result["status"] == "passed"
    assert result["samples"][0]["compile_attempts"] == 1
    assert "Coding Master" in model.calls[0][0].content
    assert "For one cohesive mutation operator" in model.calls[0][0].content
    assert "A month without a stated year remains month-only" in model.calls[0][0].content
    assert "recorded static prompt" not in model.calls[0][0].content
    assert "current fact" in model.calls[0][1].content
    assert "old fact" not in model.calls[0][1].content
    assert "action_contracts" not in model.calls[0][1].content


def test_decision_replay_matcher_supports_string_alternatives() -> None:
    expected = {"$contains_any": ["create folder", "new folder"]}

    assert decision_module._mismatches(expected, "Tap New Folder control") == []
    assert decision_module._mismatches(expected, "Tap COPY button")
