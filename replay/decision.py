"""Re-sample current Tool-Agent decisions over recorded task/frame inputs."""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from jsonschema import validate

from gui_agent.core.config import resolve_llm_config
from gui_agent.core.runtime.factory import build_platform
from gui_agent.core.tool_agent.action_guard import control_at_point
from gui_agent.core.tool_agent.contracts import DynamicActionSpec, MaterializedFrame, WorkerSpec
from gui_agent.core.tool_agent.orchestrator import compile_master_program
from gui_agent.core.tool_agent.protocol import (
    ProtocolError,
    dynamic_worker_tools,
    exactly_one_tool_call,
    image_message,
    worker_action_floor,
)
from gui_agent.prompts import load_prompt_text


_WORKER_DYNAMIC_SECTIONS = (
    "## Application knowledge",
    "## Installed applications",
    "## Session access context",
    "## Ordered multi-action mode",
    "## Worker attempt contract",
)


def _model(name: str) -> tuple[Any, str]:
    config = resolve_llm_config(name)
    return ChatOpenAI(
        model=config.model,
        api_key=config.api_key,
        base_url=config.base_url,
        timeout=config.timeout_s,
        max_retries=config.max_retries,
        temperature=0,
    ), config.model


def _artifacts(run_dir: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    trace = json.loads((run_dir / "tool_agent_trace.json").read_text(encoding="utf-8"))
    context_path = run_dir / "context.json"
    context = json.loads(context_path.read_text(encoding="utf-8")) if context_path.is_file() else {}
    return [item for item in trace.get("trace", []) if isinstance(item, dict)], context


def _report(event: dict[str, Any], label: str) -> dict[str, Any]:
    try:
        return next(
            item for item in event.get("context_reports", [])
            if item.get("kind") == "prompt_snapshot" and item.get("label") == label
        )
    except StopIteration as exc:
        raise ValueError(f"recording has no {label} prompt snapshot") from exc


def _text(parts: list[dict[str, Any]], *, omit_calls: bool = False) -> str:
    return "\n".join(
        str(part.get("text") or "") for part in parts
        if part.get("type") == "text"
        and (not omit_calls or part.get("label") != "tool_calls")
    )


def _master_task(event: dict[str, Any]) -> dict[str, Any]:
    report = _report(event, "tool_agent.master")
    human = next(role for role in report.get("roles", []) if role.get("role") == "human")
    payload = json.loads(_text(human.get("parts", [])))
    task = payload.get("task")
    if not isinstance(task, dict):
        raise ValueError("Master replay prompt has no task context")
    return task


def _worker_messages(report: dict[str, Any], screenshot: bytes) -> list[Any]:
    messages: list[Any] = []
    for role in report.get("roles", []):
        name = str(role.get("role") or "")
        parts = [part for part in role.get("parts", []) if isinstance(part, dict)]
        text = _text(parts, omit_calls=name == "assistant")
        if name == "system":
            current = load_prompt_text("task.tool_agent.worker")
            offsets = [text.find(marker) for marker in _WORKER_DYNAMIC_SECTIONS]
            offsets = [offset for offset in offsets if offset >= 0]
            suffix = "\n\n" + text[min(offsets):].lstrip() if offsets else ""
            messages.append(SystemMessage(content=current.rstrip() + suffix))
        elif name == "human":
            messages.append(
                image_message(text, screenshot)
                if any(part.get("type") == "image" for part in parts)
                else HumanMessage(content=text)
            )
        elif name == "assistant":
            calls = next(
                (json.loads(part["text"]) for part in parts if part.get("label") == "tool_calls"),
                [],
            )
            try:
                encoded = json.loads(text)
                text = str(encoded.get("text") or "") if encoded.get("type") == "text" else text
            except (AttributeError, json.JSONDecodeError):
                pass
            messages.append(AIMessage(content=text, tool_calls=calls))
        else:
            raise ValueError(f"unsupported recorded prompt role {name!r}")
    return messages


def _literal(call: ast.Call | None, name: str, default: Any = None) -> Any:
    keyword = next((item for item in (call.keywords if call else []) if item.arg == name), None)
    try:
        return ast.literal_eval(keyword.value) if keyword else default
    except (TypeError, ValueError):
        return default


def _master_shape(source: str, *, reviewed: bool = True) -> dict[str, Any]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        tree = ast.parse("pass")
        reviewed = False
    calls = sorted(
        (
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "ctx"
        ),
        key=lambda node: (node.lineno, node.col_offset),
    )
    workers = [call for call in calls if call.func.attr == "gui_worker"]
    profiles = []
    for call in workers:
        profile = _literal(call, "profile")
        profiles.append(profile or ("collector" if _literal(call, "data_requirements", []) else "operator"))
    finish = next((call for call in calls if call.func.attr == "finish"), None)
    return {
        "reviewed": reviewed,
        "worker_count": len(workers),
        "worker_profiles": profiles,
        "api_calls": [call.func.attr for call in calls],
        "finish_effect": _literal(finish, "effect", ""),
    }


def _mismatches(expected: Any, actual: Any, path: str = "$") -> list[str]:
    if not isinstance(expected, dict):
        return [] if expected == actual else [f"{path}: expected {expected!r}, got {actual!r}"]
    if not isinstance(actual, dict):
        return [f"{path}: expected object, got {type(actual).__name__}"]
    errors = []
    for key, value in expected.items():
        errors.extend(
            [f"{path}.{key}: missing"]
            if key not in actual else _mismatches(value, actual[key], f"{path}.{key}")
        )
    return errors


def _result(kind: str, model: str, expectation: dict[str, Any], samples: list[dict], **where: Any) -> dict:
    passed = sum(item["passed"] for item in samples)
    return {
        "status": "passed" if passed == len(samples) else "failed",
        "summary": f"{kind.capitalize()} decision replay: {passed}/{len(samples)} sample(s) passed.",
        "kind": kind,
        "model": model,
        **where,
        "expectation": expectation,
        "samples": samples,
        "uses_llm": True,
        "uses_device": False,
    }


def replay_master_decision(
    run_dir: Path,
    *,
    samples: int = 1,
    expectation: dict[str, Any] | None = None,
    llm: Any = None,
) -> dict[str, Any]:
    if samples < 1:
        raise ValueError("samples must be positive")
    events, _ = _artifacts(run_dir)
    attempts = [event for event in events if event.get("event") == "master_compile_attempt"]
    selected = next((event for event in reversed(attempts) if not event.get("diagnostics")), None)
    if selected is None:
        raise ValueError("recording has no reviewed Master compile attempt")
    expected = expectation or _master_shape(str(selected.get("source") or ""))
    model, model_name = (llm, type(llm).__name__) if llm is not None else _model("tool_agent.master")
    results = []
    for number in range(1, samples + 1):
        try:
            program = compile_master_program(
                llm=model,
                system_prompt=load_prompt_text("task.tool_agent.master"),
                task_context=_master_task(selected),
                max_attempts=5,
            )
            decision = _master_shape(program.source)
            source, diagnostics = program.source, []
        except Exception as exc:  # noqa: BLE001 - compile failure is a replay verdict
            decision = _master_shape("", reviewed=False)
            source, diagnostics = "", [f"{type(exc).__name__}: {exc}"]
        errors = _mismatches(expected, decision)
        results.append({
            "sample": number,
            **decision,
            "diagnostics": diagnostics,
            "source": source,
            "passed": not errors,
            "errors": errors,
        })
    return _result("master", model_name, expected, results, attempt=selected.get("attempt"))


def _frame_number(value: str | int) -> int:
    text = str(value).removeprefix("frame:")
    number = int(text)
    if number < 1:
        raise ValueError("worker frame must be positive")
    return number


def _worker_spec(events: list[dict], selected: dict) -> WorkerSpec:
    candidates = [
        event for event in events
        if event.get("index", -1) < selected.get("index", 0)
        and event.get("worker_id") == selected.get("worker_id")
        and event.get("event") in {"master_worker_dispatch", "master_worker_redelegated"}
        and isinstance(event.get("spec"), dict)
    ]
    if not candidates:
        raise ValueError(f"recording has no WorkerSpec for {selected.get('worker_id')!r}")
    return WorkerSpec.model_validate(candidates[-1]["spec"])


def _worker_actions(run_dir: Path, events: list[dict], context: dict, selected: dict, spec: WorkerSpec) -> list[DynamicActionSpec]:
    store_path = run_dir / "tool_agent_data_store.json"
    values = (
        json.loads(store_path.read_text(encoding="utf-8")).get("values", {})
        if store_path.is_file() else {}
    )
    actions = []
    for action in spec.actions:
        fixed = dict(action.fixed_args)
        for argument, binding in action.input_args.items():
            value = values[spec.input_refs[binding.input]]
            for part in binding.path:
                value = value[part]
            fixed[argument] = value
        actions.append(action.model_copy(update={"fixed_args": fixed, "input_args": {}}))
    capabilities = build_platform(str(context.get("platform") or "browser")).tool_agent_capabilities
    actions.extend(worker_action_floor(capabilities))
    known = {action.name for action in actions}
    for event in events:
        if event.get("index", -1) >= selected.get("index", 0):
            break
        if (
            event.get("event") == "worker_action_patch"
            and event.get("worker_id") == selected.get("worker_id")
            and isinstance(event.get("action"), dict)
        ):
            action = DynamicActionSpec.model_validate(event["action"])
            if action.name not in known:
                actions.append(action)
                known.add(action.name)
    return actions


def _screenshot(run_dir: Path, frame: int, observation: dict) -> bytes:
    recorded = Path(str(observation.get("screenshot_path") or ""))
    candidates = (recorded, run_dir / recorded.name, run_dir / f"screenshot_tool_agent_{frame}.png")
    try:
        return next(path for path in candidates if path.is_file()).read_bytes()
    except StopIteration as exc:
        raise FileNotFoundError(f"recording has no screenshot for frame:{frame}") from exc


def _action_names(event: dict) -> list[str]:
    if event.get("tool") != "continue_with_actions":
        return [str(event.get("tool") or "")]
    return [
        str(item.get("name") or "")
        for item in event.get("args", {}).get("actions", [])
        if isinstance(item, dict)
    ]


def _visible_action_target(
    action: dict[str, Any],
    frame: MaterializedFrame,
) -> str:
    args = action.get("args") if isinstance(action.get("args"), dict) else {}
    control = control_at_point(args, frame)
    label = (
        str(control.get("label") or control.get("value") or control.get("kind") or "")
        if control is not None
        else ""
    )
    return label or str(action.get("name") or "")


def _worker_decision(
    response: Any,
    actions: list[DynamicActionSpec],
    tools: dict[str, dict[str, Any]],
    frame: MaterializedFrame,
) -> dict[str, Any]:
    call = exactly_one_tool_call(response)
    tool = tools.get(call["name"])
    if tool is None:
        raise ProtocolError(f"unknown Worker tool {call['name']!r}")
    validate(instance=call["args"], schema=tool["function"]["parameters"])
    state = call["args"].get("state")
    names = [call["name"]]
    ordered = [{"name": call["name"], "args": call["args"]}]
    if call["name"] == "continue_with_actions":
        ordered = call["args"].get("actions") or []
        ordered = json.loads(ordered) if isinstance(ordered, str) else ordered
        names = [str(item.get("name") or "") for item in ordered]
    capabilities = {action.name: action.capability for action in actions}
    return {
        "tool": call["name"],
        "actions": names,
        "action_capabilities": [capabilities.get(name, name) for name in names],
        "action_targets": [
            _visible_action_target(action, frame)
            for action in ordered
            if isinstance(action, dict)
        ],
        "state_status": str(state.get("status") or ""),
        "state_summary": str(state.get("summary") or ""),
        "args": call["args"],
    }


def replay_worker_decision(
    run_dir: Path,
    *,
    frame: str | int,
    samples: int = 1,
    expectation: dict[str, Any] | None = None,
    llm: Any = None,
) -> dict[str, Any]:
    if samples < 1:
        raise ValueError("samples must be positive")
    frame_no = _frame_number(frame)
    events, context = _artifacts(run_dir)
    selected = next((
        event for event in reversed(events)
        if event.get("event") == "worker_decision" and event.get("frame_id") == f"frame:{frame_no}"
    ), None)
    if selected is None:
        raise ValueError(f"recording has no Worker decision for frame:{frame_no}")
    observation = json.loads(
        (run_dir / f"observation_tool_agent_{frame_no}.json").read_text(encoding="utf-8")
    )
    materialized = MaterializedFrame.model_validate(observation)
    spec = _worker_spec(events, selected)
    actions = _worker_actions(run_dir, events, context, selected, spec)
    ready = bool(spec.data_requirements) and any(
        item.requirement_id == spec.data_requirements[0].id
        and item.coverage.get("scope_status") == "met"
        and item.coverage.get("status") == "complete"
        for item in materialized.collections
    )
    completion = "operator" if spec.profile == "operator" else "collector" if ready else "unavailable"
    started = next((event for event in events if event.get("event") == "runtime_started"), {})
    tools = dynamic_worker_tools(
        actions,
        completion_mode=completion,
        action_envelope=bool(started.get("multi_action")),
    )
    messages = _worker_messages(
        _report(selected, "tool_agent.worker"),
        _screenshot(run_dir, frame_no, observation),
    )
    capabilities = {action.name: action.capability for action in actions}
    expected = expectation or {
        "tool": str(selected.get("tool") or ""),
        "action_capabilities": [capabilities.get(name, name) for name in _action_names(selected)],
    }
    model, model_name = (llm, type(llm).__name__) if llm is not None else _model("tool_agent.worker")
    bound = model.bind_tools(
        tools,
        tool_choice="required",
        parallel_tool_calls=False,
        extra_body={"enable_thinking": False},
    )
    tools_by_name = {tool["function"]["name"]: tool for tool in tools}
    results = []
    for number in range(1, samples + 1):
        replay_messages, repairs = list(messages), 0
        for protocol_attempt in range(2):
            response = bound.invoke(replay_messages)
            try:
                decision = _worker_decision(
                    response,
                    actions,
                    tools_by_name,
                    materialized,
                )
                break
            except Exception as exc:  # noqa: BLE001 - mirrors one production repair
                if protocol_attempt:
                    raise
                repairs += 1
                replay_messages.extend([response, HumanMessage(content=(
                    f"Protocol repair: {exc}. On this SAME frame, emit exactly one required "
                    "tool call including its state field. No action was executed."
                ))])
        errors = _mismatches(expected, decision)
        results.append({
            "sample": number,
            **decision,
            "protocol_repairs": repairs,
            "passed": not errors,
            "errors": errors,
        })
    return _result(
        "worker",
        model_name,
        expected,
        results,
        frame_id=f"frame:{frame_no}",
        worker_id=selected.get("worker_id"),
        step=selected.get("step"),
    )


def main() -> int:
    from dotenv import load_dotenv

    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--master", action="store_true")
    mode.add_argument("--worker-frame", metavar="N")
    parser.add_argument("--samples", type=int, default=1)
    parser.add_argument("--expect-json", default="")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()
    if not 1 <= args.samples <= 10:
        parser.error("--samples must be between 1 and 10")
    expected = json.loads(args.expect_json) if args.expect_json else None
    run_dir = args.run_dir.expanduser().resolve()
    result = (
        replay_master_decision(run_dir, samples=args.samples, expectation=expected)
        if args.master else replay_worker_decision(
            run_dir, frame=args.worker_frame, samples=args.samples, expectation=expected
        )
    )
    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"[{result['status'].upper()}] {result['summary']}")
        for sample in result["samples"]:
            decision = sample.get("tool") or ",".join(sample.get("api_calls", []))
            print(f"sample={sample['sample']} passed={sample['passed']} decision={decision}")
            for error in sample["errors"]:
                print(f"  {error}")
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
