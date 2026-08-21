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
        "## WorkerMemory (recorded)\nold memory\n\n"
        "## Current MaterializedFrame (compact semantic projection)\n"
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
                    "text": (
                        "recorded static prompt\n\n## Application knowledge\n"
                        "old fact\n\n## Nested old app section\nold nested fact\n\n"
                        "## Worker attempt contract\n{}"
                    ),
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
    repair_history: bool = False,
    spec: WorkerSpec | None = None,
    actions: list[DynamicActionSpec] | None = None,
) -> Path:
    actions = actions or [DynamicActionSpec(
        name="scroll",
        capability="scroll",
        description="recorded static action description",
        exposed_args=["direction", "amount"],
    )]
    if controls:
        actions.append(DynamicActionSpec(
            name="tap",
            capability="tap",
            description="Tap a visible control",
            exposed_args=["x", "y", "description"],
        ))
    spec = spec or WorkerSpec.model_validate({
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
        "controls": controls or [],
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


def test_worker_replay_compares_equivalent_actions_by_capability(
    tmp_path, monkeypatch,
) -> None:
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
    context_path = run_dir / "context.json"
    context_path.write_text(json.dumps({
        "platform": "android",
        "goal": "test",
        "knowledge": {"app_name": "Files"},
    }), encoding="utf-8")
    knowledge = SimpleNamespace(
        worker_context=lambda: "current worker fact",
        orchestrator_context=lambda _goal: "current master fact",
    )
    monkeypatch.setattr(decision_module, "load_knowledge_for_app", lambda *_: knowledge)

    result = replay_worker_decision(run_dir, frame=1, llm=model)

    assert result["status"] == "passed"
    assert result["expectation"] == {
        "tool": "continue_with_actions",
        "action_capabilities": ["scroll"],
    }
    assert result["samples"][0]["actions"] == ["scroll"]
    assert result["samples"][0]["action_capabilities"] == ["scroll"]
    assert result["samples"][0]["first_action_capability"] == "scroll"
    assert result["samples"][0]["first_action_target"] == "scroll"
    assert {tool["function"]["name"] for tool in model.bound_tools} >= {
        "continue_with_actions",
        "complete",
        "report_blocked",
    }
    assert "recorded static prompt" not in model.calls[0][0].content
    assert "current worker fact" in model.calls[0][0].content
    assert "old fact" not in model.calls[0][0].content


def test_worker_replay_can_replace_recorded_memory_with_production_projection(
    tmp_path,
) -> None:
    run_dir = _worker_run(
        tmp_path,
        recorded_tool="continue_with_actions",
        recorded_actions=[{"name": "scroll", "args": {}}],
    )
    model = _RecordedModel(_tool_call("continue_with_actions", {
        "state": _state(),
        "actions": [{
            "name": "scroll",
            "args": {"direction": "down", "description": "Current collection"},
        }],
    }))
    projection = "## WorkerMemory (runtime-compacted current belief)\n- stable belief"

    result = replay_worker_decision(
        run_dir,
        frame=1,
        worker_memory_override=projection,
        llm=model,
    )

    assert result["status"] == "passed"
    human = "\n".join(
        str(item.get("text") or "")
        for item in model.calls[0][-1].content
        if isinstance(item, dict) and item.get("type") == "text"
    )
    assert projection in human
    assert "old memory" not in human
    assert "Execute the binding `approach`" in model.calls[0][0].content
    assert "old nested fact" not in model.calls[0][0].content
    assert "## Worker attempt contract" not in model.calls[0][0].content
    turn_context = model.calls[0][1].content[0]["text"]
    assert "stale" not in turn_context


def test_worker_replay_preserves_memory_from_current_production_order(
    tmp_path,
) -> None:
    run_dir = _worker_run(
        tmp_path,
        recorded_tool="continue_with_actions",
        recorded_actions=[{"name": "scroll", "args": {}}],
    )
    trace_path = run_dir / "tool_agent_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    human = trace["trace"][-1]["context_reports"][0]["roles"][1]
    human["parts"][0]["text"] = (
        "## Current MaterializedFrame frame:old\n{\"title\": \"stale\"}\n\n"
        "## Current Worker attempt\nold contract\n\n"
        "## WorkerMemory (runtime-compacted current belief)\nretained traversal\n\n"
        "## Current frame anchor (authoritative now)\nold anchor"
    )
    trace_path.write_text(json.dumps(trace), encoding="utf-8")
    model = _RecordedModel(_tool_call("continue_with_actions", {
        "state": _state(),
        "actions": [{
            "name": "scroll",
            "args": {"direction": "down", "description": "Current collection"},
        }],
    }))

    result = replay_worker_decision(run_dir, frame=1, llm=model)

    assert result["status"] == "passed"
    turn_context = model.calls[0][1].content[0]["text"]
    assert "retained traversal" in turn_context
    assert "stale" not in turn_context
    assert "old contract" not in turn_context
    assert turn_context.count("## Current frame anchor") == 1
    assert '"inspection_traversal": "open"' in turn_context
    assert '"frame_id": "frame:1"' in turn_context
    assert turn_context.index("Current MaterializedFrame") < turn_context.index(
        "Current Worker attempt"
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


def test_worker_replay_rebuilds_current_input_binding_description(tmp_path) -> None:
    spec = WorkerSpec.model_validate({
        "profile": "operator",
        "goal": "Open the bound record",
        "success_criteria": ["The bound record is open"],
        "input_refs": {"query": "result:1"},
        "input_bindings": [{
            "name": "enter_query",
            "input": "query",
            "target": "text_input",
            "description": "Exact identity for the target record",
        }],
        "strategy": {"approach": "record detail source"},
    })
    run_dir = _worker_run(
        tmp_path,
        recorded_tool="continue_with_actions",
        recorded_actions=[{"name": "enter_query", "args": {"x": 500, "y": 100}}],
        controls=[{
            "kind": "aria_combobox", "label": "Search",
            "rect": {"x": 500, "y": 100, "w": 300, "h": 40},
        }],
        spec=spec,
        actions=[DynamicActionSpec(
            name="enter_query",
            capability="type",
            description="recorded ambiguous binding description",
            fixed_args={"text": "private value"},
            exposed_args=["x", "y", "description"],
        )],
    )
    model = _RecordedModel(_tool_call("continue_with_actions", {
        "state": _state(),
        "actions": [{
            "name": "enter_query",
            "args": {"x": 500, "y": 100, "description": "Search input"},
        }],
    }))

    result = replay_worker_decision(run_dir, frame=1, llm=model)

    assert result["status"] == "passed"
    envelope = next(
        tool for tool in model.bound_tools
        if tool["function"]["name"] == "continue_with_actions"
    )
    variants = envelope["function"]["parameters"]["properties"]["actions"]["items"]["oneOf"]
    binding = next(
        item for item in variants
        if item["properties"]["name"]["const"] == "enter_query"
    )
    assert "Replace the value of one visible input control" in binding["description"]
    assert "recorded ambiguous binding description" not in binding["description"]


def test_worker_replay_reports_grounded_visible_action_target(tmp_path) -> None:
    run_dir = _worker_run(
        tmp_path,
        recorded_tool="continue_with_actions",
        recorded_actions=[{"name": "tap", "args": {"x": 180, "y": 246}}],
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
            "name": "tap",
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
    assert result["uses_llm"] is True


def test_worker_replay_marks_redacted_value_action_unavailable(tmp_path) -> None:
    run_dir = _worker_run(
        tmp_path,
        recorded_tool="continue_with_actions",
        recorded_actions=[{
            "name": "type",
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
    monkeypatch.setattr(decision_module, "build_llm", lambda _name: (model, config))

    result = replay_worker_decision(run_dir, frame=1)

    assert result["status"] == "passed"
    assert result["expectation"]["action_capabilities"] == ["scroll"]
    assert result["samples"][0]["actions"] == ["scroll"]
    assert result["samples"][0]["protocol_repairs"] == 1
    assert model.bound_tools == []
    assert any("Decision transport" in message.content for message in model.calls[0])
    assert any('"maxItems":5' in message.content for message in model.calls[0])
    assert "JSON object with only tool and args" in model.calls[1][-1].content


def test_master_replay_recovers_failed_recording_with_current_prompt_and_knowledge(
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
        "diagnostics": ["recorded compile failure"],
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
            "data_filter_values": [],
            "finish_effect": "mutation",
        },
    )

    assert result["status"] == "passed"
    assert result["samples"][0]["compile_attempts"] == 1
    assert "Coding Master" in model.calls[0][0].content
    assert "recorded static prompt" not in model.calls[0][0].content
    assert "current fact" in model.calls[0][1].content
    assert "old fact" not in model.calls[0][1].content
    assert "action_contracts" not in model.calls[0][1].content
