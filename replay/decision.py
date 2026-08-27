"""Re-sample current Tool-Agent decisions over recorded task/frame inputs."""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from gui_agent.core.config import resolve_llm_config
from gui_agent.core.runtime.clock import PlatformTimeSnapshot
from gui_agent.core.self_learning.app_summary import load_knowledge_for_app
from gui_agent.core.tool_agent.contracts import (
    DynamicActionSpec,
    MaterializedFrame,
    WorkerStateSnapshot,
    WorkerSpec,
    approach_atomic_action_count,
    approach_is_procedural,
)
from gui_agent.core.tool_agent.orchestrator import (
    compile_master_program,
)
from gui_agent.core.tool_agent.protocol import (
    MAX_ORDERED_ACTIONS,
    CompleteReadyWorkerArgs,
    bind_actor_decision_transport,
    decode_actor_action,
    dynamic_actor_tools,
    generic_action_spec,
    image_message,
    model_tool,
    worker_attempt_contract,
    worker_frame_tools,
    worker_profile_rules,
)
from gui_agent.core.tool_agent.runtime import ToolAgentRuntime
from gui_agent.core.tool_agent.state_trace import (
    state_actor_markdown,
)
from gui_agent.prompts import load_prompt_text
from llm.provider_config import (
    build_chat_model,
    chat_request_kwargs,
)


_REDACTED_ACCESS_VALUE = "[session access value redacted]"


def _model(name: str) -> tuple[Any, Any]:
    config = resolve_llm_config(name)
    return build_chat_model(config), config


def _selected_model(name: str, llm: Any) -> tuple[Any, Any, str]:
    if llm is not None:
        return llm, None, type(llm).__name__
    model, config = _model(name)
    return model, config, config.model


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


def _actor_report(event: dict[str, Any]) -> dict[str, Any]:
    for label in (
        "tool_agent.actor",
        "tool_agent.worker.protocol_repair",
        "tool_agent.worker",
    ):
        try:
            return _report(event, label)
        except ValueError:
            pass
    raise ValueError("recording has no Actor prompt snapshot")


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
    task = dict(task)
    if "relative_date_offsets" not in task:
        try:
            snapshot = PlatformTimeSnapshot.model_validate(task["task_reference_time"])
        except (KeyError, ValueError):
            pass
        else:
            task["relative_date_offsets"] = snapshot.relative_date_offsets()
    return task


def _current_app_knowledge(context: dict[str, Any]) -> list[Any]:
    platform = str(context.get("platform") or "browser")
    summary = context.get("knowledge") or {}
    apps = summary.get("apps") or ([summary] if summary.get("app_name") else [])
    return [
        value
        for item in apps
        if isinstance(item, dict)
        and (name := str(item.get("app_name") or ""))
        and (value := load_knowledge_for_app(name, platform)) is not None
    ]


def _current_master_task(task: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Refresh mutable knowledge while preserving the recorded task inputs."""

    goal = str(context.get("goal") or "")
    knowledge = "\n\n".join(
        value.orchestrator_context(goal)
        for value in _current_app_knowledge(context)
    )
    current = {key: value for key, value in task.items() if key != "application_knowledge"}
    if isinstance(current.get("platform"), dict):
        current["platform"] = {
            key: value
            for key, value in current["platform"].items()
            if key != "action_contracts"
        }
    if knowledge:
        current["application_knowledge"] = knowledge
    return current


def _validated_recorded_state(
    value: Any,
) -> dict[str, Any]:
    raw = dict(value) if isinstance(value, dict) else {}
    if "markdown" in raw:
        return WorkerStateSnapshot.model_validate(raw).model_dump(mode="json")
    if not str(raw.get("summary") or "").strip():
        raise ValueError("recorded State has no summary")
    targets: dict[str, Any] = {}
    sections: list[str] = []
    for ref, target in (raw.get("targets") or {}).items():
        if not isinstance(target, dict):
            continue
        targets[str(ref)] = {
            key: target[key]
            for key in ("identity", "visibility", "owned_region_visibility")
            if key in target
        }
        facts = [
            f"- Identity: {target.get('identity') or ref}",
            *[
                f"- {name}: {item.get('value')!r}"
                for name, item in (target.get("properties") or {}).items()
                if isinstance(item, dict)
            ],
        ]
        sections.append(f"### {ref}\n" + "\n".join(facts))
    legacy_facts = [
        str(item)
        for item in (raw.get("established_facts") or [])
        if str(item).strip()
    ]
    legacy_facts.extend(
        str(item.get("statement") or "")
        for item in (raw.get("memory_updates") or [])
        if isinstance(item, dict) and str(item.get("statement") or "").strip()
    )
    if legacy_facts:
        sections.append("### Recorded observations\n" + "\n".join(
            f"- {statement}" for statement in legacy_facts
        ))
    factual = {
        "summary": str(raw.get("summary") or "Recorded observed facts."),
        "frame_id": str(raw.get("frame_id") or ""),
        "surface": raw.get("surface"),
        "targets": targets,
        "markdown": "\n\n".join(sections),
    }
    return WorkerStateSnapshot.model_validate(factual).model_dump(mode="json")


def _actor_messages(
    report: dict[str, Any],
    screenshot: bytes,
    *,
    attempt_contract: str,
    state: dict[str, Any],
    application_knowledge: str = "",
    image_scale: float = 1.0,
    profile: str | None = None,
) -> list[Any]:
    system = load_prompt_text("task.tool_agent.actor").rstrip()
    if application_knowledge:
        system += (
            "\n\n## Application knowledge (interface mechanics only)\n"
            + application_knowledge
        )
    profile_rules = worker_profile_rules(profile)
    if profile_rules:
        system += "\n\n" + profile_rules
    actor_input = "\n\n".join((
        attempt_contract,
        "## Continuous observed fact memory",
        state_actor_markdown(WorkerStateSnapshot.model_validate(state)),
    ))
    messages: list[Any] = [
        SystemMessage(content=system),
        image_message(actor_input, screenshot, scale=image_scale),
    ]
    # Preserve only same-frame repair history from the recording. Base prompt
    # content is rebuilt from the current Actor contract above.
    for role in report.get("roles", [])[2:]:
        name = str(role.get("role") or "")
        parts = [part for part in role.get("parts", []) if isinstance(part, dict)]
        text = _text(parts, omit_calls=name == "assistant")
        if name == "human":
            messages.append(HumanMessage(content=text))
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
    calls = sorted((
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "ctx"
    ), key=lambda node: (node.lineno, node.col_offset))
    workers = [call for call in calls if call.func.attr == "gui_worker"]
    profiles = []
    cardinalities = []
    field_counts = []
    required_field_counts = []
    procedural_approaches = []
    approach_action_counts = []
    unresolved_input_counts = []
    for call in workers:
        profile = _literal(call, "profile")
        requirements = _literal(call, "data_requirements", [])
        approach = str(_literal(call, "approach", "") or "")
        unresolved_inputs = _literal(call, "unresolved_inputs", {})
        profiles.append(profile or ("collector" if requirements else "operator"))
        procedural_approaches.append(approach_is_procedural(approach))
        approach_action_counts.append(approach_atomic_action_count(approach))
        unresolved_input_counts.append(
            len(unresolved_inputs) if isinstance(unresolved_inputs, dict) else -1
        )
        cardinalities.extend(
            str(item.get("cardinality") or "many")
            for item in requirements
            if isinstance(item, dict)
        )
        for item in requirements:
            if not isinstance(item, dict):
                continue
            schema = item.get("row_schema") or {}
            field_counts.append(len(schema.get("properties") or {}))
            required_field_counts.append(len(schema.get("required") or []))
    finish = next((call for call in calls if call.func.attr == "finish"), None)
    return {
        "reviewed": reviewed,
        "worker_count": len(workers),
        "worker_profiles": profiles,
        "data_cardinalities": cardinalities,
        "data_field_counts": field_counts,
        "required_data_field_counts": required_field_counts,
        "procedural_approaches": procedural_approaches,
        "approach_action_counts": approach_action_counts,
        "unresolved_input_counts": unresolved_input_counts,
        "api_calls": [call.func.attr for call in calls],
        "finish_effect": _literal(finish, "effect", ""),
    }


def _mismatches(expected: Any, actual: Any, path: str = "$") -> list[str]:
    if isinstance(expected, dict) and set(expected) == {"$contains_any"}:
        alternatives = expected["$contains_any"]
        if not isinstance(actual, str):
            return [f"{path}: expected string, got {type(actual).__name__}"]
        if not isinstance(alternatives, list) or not all(
            isinstance(item, str) and item for item in alternatives
        ):
            return [f"{path}: $contains_any requires non-empty string alternatives"]
        folded = actual.casefold()
        return [] if any(item.casefold() in folded for item in alternatives) else [
            f"{path}: expected one of {alternatives!r} within {actual!r}"
        ]
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return [f"{path}: expected array, got {type(actual).__name__}"]
        if len(expected) != len(actual):
            return [f"{path}: expected {expected!r}, got {actual!r}"]
        errors = []
        for index, value in enumerate(expected):
            errors.extend(_mismatches(value, actual[index], f"{path}[{index}]"))
        return errors
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
    events, context = _artifacts(run_dir)
    attempts = [event for event in events if event.get("event") == "master_compile_attempt"]
    selected = next((event for event in reversed(attempts) if not event.get("diagnostics")), None)
    if selected is None:
        if not attempts:
            raise ValueError("recording has no Master compile attempt")
        if expectation is None:
            raise ValueError(
                "recording has no reviewed Master compile attempt; pass an explicit "
                "expectation to replay the failed Master decision"
            )
        selected = attempts[-1]
    expected = expectation or _master_shape(str(selected.get("source") or ""))
    model, _, model_name = _selected_model("tool_agent.master", llm)
    task = _current_master_task(_master_task(selected), context)
    results = []
    for number in range(1, samples + 1):
        compile_history = []
        try:
            program = compile_master_program(
                llm=model,
                system_prompt=load_prompt_text("task.tool_agent.master"),
                task_context=task,
                max_attempts=5,
                on_event=lambda event, payload: compile_history.append({
                    "attempt": payload.get("attempt"),
                    "source": str(payload.get("source") or ""),
                    "diagnostics": list(payload.get("diagnostics") or []),
                    "llm_elapsed_s": payload.get("llm_elapsed_s"),
                    "token_usage": payload.get("token_usage") or {},
                }) if event == "master_compile_attempt" else None,
            )
            decision = {
                **_master_shape(program.source),
                "compile_attempts": program.attempts,
            }
            source, diagnostics = program.source, []
        except Exception as exc:  # noqa: BLE001 - compile failure is a replay verdict
            decision = {
                **_master_shape("", reviewed=False),
                "compile_attempts": 0,
            }
            source, diagnostics = "", [f"{type(exc).__name__}: {exc}"]
        errors = _mismatches(expected, decision)
        results.append({
            "sample": number,
            **decision,
            "compile_history": compile_history,
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


def _screenshot(run_dir: Path, frame: int, observation: dict) -> bytes:
    recorded = Path(str(observation.get("screenshot_path") or ""))
    candidates = (recorded, run_dir / recorded.name, run_dir / f"screenshot_tool_agent_{frame}.png")
    try:
        return next(path for path in candidates if path.is_file()).read_bytes()
    except StopIteration as exc:
        raise FileNotFoundError(f"recording has no screenshot for frame:{frame}") from exc


def _unavailable(selected: dict[str, Any], frame_no: int, summary: str) -> dict[str, Any]:
    return {
        "status": "unavailable", "summary": summary, "kind": "worker",
        "model": "not invoked", "frame_id": f"frame:{frame_no}",
        "worker_id": selected.get("worker_id"), "step": selected.get("step"),
        "expectation": {}, "samples": [], "uses_llm": False, "uses_device": False,
    }


def _worker_decision(
    response: Any,
    actions: list[DynamicActionSpec],
    tools: dict[str, dict[str, Any]],
    state: dict[str, Any],
    *,
    action_protocol: str = "tool_call",
) -> dict[str, Any]:
    # Completion is a State decision, not an Actor tool, but a recorded frame may
    # replay a terminal `complete`; accept it as a valid decode-side tool so the
    # re-sampler can reproduce a recorded completion decision.
    decode_tools = dict(tools)
    decode_tools["complete"] = model_tool(
        "complete",
        "Declare the Goal Contract established now.",
        CompleteReadyWorkerArgs,
    )
    call, raw_state, calls = decode_actor_action(
        response,
        protocol=action_protocol,
        tools=decode_tools,
    )
    if raw_state is not None:
        raise ValueError("Actor must not emit state; State is authoritative")
    if call["name"] == "continue_with_actions":
        ToolAgentRuntime._validate_multi_action_calls(calls, actions)
    names = [call["name"]]
    if call["name"] == "continue_with_actions":
        names = [str(item.get("name") or "") for item in calls]
    capabilities = {action.name: action.capability for action in actions}
    return {
        "tool": call["name"],
        "actions": names,
        "action_capabilities": [capabilities.get(name, name) for name in names],
        "action_semantics": _action_semantics(calls, capabilities),
        "state": state,
        "state_summary": state.get("summary", ""),
        "args": call["args"],
    }


_REPLAY_SEMANTIC_ACTION_FIELDS = (
    "app",
    "text",
    "url",
    "direction",
    "amount",
    "duration_ms",
    "target_area",
    "option",
    "key",
)


def _action_semantics(
    calls: list[dict[str, Any]],
    capabilities: dict[str, str],
) -> list[dict[str, Any]]:
    """Project replay-stable action meaning while ignoring wording and point jitter."""
    return [
        {
            "capability": capabilities.get(name, name),
            **{key: args[key] for key in _REPLAY_SEMANTIC_ACTION_FIELDS
               if args.get(key) not in (None, "")},
        }
        for call in calls
        if (name := str(call.get("name") or ""))
        and isinstance((args := call.get("args") or {}), dict)
    ]


def replay_worker_decision(
    run_dir: Path,
    *,
    frame: str | int,
    samples: int = 1,
    expectation: dict[str, Any] | None = None,
    visible_commit_control: str = "",
    llm: Any = None,
) -> dict[str, Any]:
    if samples < 1:
        raise ValueError("samples must be positive")
    frame_no = _frame_number(frame)
    events, context = _artifacts(run_dir)
    selected = next((
        event for event in reversed(events)
        if event.get("event") == "worker_decision"
        and event.get("frame_id") == f"frame:{frame_no}"
    ), None)
    if selected is None:
        failure = next((
            event for event in events
            if event.get("event") == "worker_protocol_error"
            and int(event.get("step") or 0) == frame_no
            and int(event.get("attempt") or 1) == 1
        ), None)
        prior = next((
            event for event in reversed(events)
            if failure is not None
            and event.get("event") == "worker_decision"
            and event.get("worker_id") == failure.get("worker_id")
            and int(event.get("step") or 0) < frame_no
        ), None)
        if expectation is None or failure is None or prior is None:
            raise ValueError(f"recording has no Worker decision for frame:{frame_no}")
        selected = {
            **failure,
            "event": "worker_decision",
            "frame_id": f"frame:{frame_no}",
            "replay_context": prior.get("replay_context"),
            "state": next((
                event.get("state") for event in reversed(events)
                if event.get("event") == "worker_state"
                and event.get("frame_id") == f"frame:{frame_no}"
            ), prior.get("state")),
            "tool": str(expectation.get("tool") or ""),
            "args": {},
        }
    replay_context = selected.get("replay_context")
    if not isinstance(replay_context, dict) or replay_context.get("version") != 2:
        return _unavailable(
            selected, frame_no, "Worker decision replay requires trace schema v2."
        )
    prior_action = next((
        event for event in reversed(events)
        if int(event.get("index") or 0) < int(selected.get("index") or 0)
        and event.get("event") == "runtime_action"
    ), None)
    prior_no_effect_scroll = bool(
        prior_action
        and prior_action.get("tool") == "scroll"
        and prior_action.get("no_effect") is True
    )
    if _REDACTED_ACCESS_VALUE in json.dumps(
        selected.get("args") or {},
        ensure_ascii=False,
    ):
        return _unavailable(
            selected, frame_no,
            "Worker decision replay requires a value redacted from run artifacts.",
        )
    observation = json.loads(
        (run_dir / f"observation_tool_agent_{frame_no}.json").read_text(encoding="utf-8")
    )
    spec = WorkerSpec.model_validate(replay_context["worker_spec"], extra="ignore")
    materialized = MaterializedFrame.model_validate(observation)
    actions = []
    for raw_action in replay_context.get("actions") or []:
        action = DynamicActionSpec.model_validate(raw_action)
        actions.append(
            generic_action_spec(action.capability)
            if action.name == action.capability
            and not action.fixed_args
            and not action.input_args
            else action
        )
    assessment = worker_frame_tools(
        spec, actions, materialized,
    )
    state = _validated_recorded_state(selected.get("state"))
    model, model_config, model_name = _selected_model("tool_agent.worker", llm)
    multi_action = bool(replay_context.get("multi_action"))
    max_ordered_actions = MAX_ORDERED_ACTIONS if multi_action else 1
    action_protocol = str(getattr(model_config, "action_protocol", "tool_call"))
    frame_actions = assessment.allowed_actions
    tools = dynamic_actor_tools(
        frame_actions,
        action_envelope=multi_action,
        max_ordered_actions=max_ordered_actions,
    )
    capabilities = {action.name: action.capability for action in actions}
    action_names = (
        [
            str(item.get("name") or "")
            for item in selected.get("args", {}).get("actions", [])
            if isinstance(item, dict)
        ]
        if selected.get("tool") == "continue_with_actions"
        else [str(selected.get("tool") or "")]
    )
    recorded_capabilities = [capabilities.get(name, name) for name in action_names]
    if selected.get("tool") == "continue_with_actions":
        recorded_capabilities = recorded_capabilities[:max_ordered_actions]
    recorded_calls = selected.get("args", {}).get("actions") or []
    if selected.get("tool") != "continue_with_actions":
        recorded_calls = [{
            "name": str(selected.get("tool") or ""),
            "args": selected.get("args") or {},
        }]
    elif not isinstance(recorded_calls, list):
        recorded_calls = []
    recorded_calls = [
        item for item in recorded_calls[:max_ordered_actions]
        if isinstance(item, dict)
    ]
    expected = expectation if expectation is not None else {
        "tool": str(selected.get("tool") or ""),
        "action_capabilities": recorded_capabilities,
        "action_semantics": _action_semantics(recorded_calls, capabilities),
    }
    attempt_contract = worker_attempt_contract(
        spec,
        attempted_action=bool(replay_context.get("executed_tools")),
    )
    if prior_no_effect_scroll:
        attempt_contract += (
            "\n\n## Same-frame runtime feedback\n"
            + json.dumps({
                "status": "collection_traversal_boundary",
                "boundary_direction": str(
                    (prior_action.get("args") or {}).get("direction") or
                    prior_action.get("direction") or "attempted_direction"
                ),
                "same_direction_exhausted": True,
                "action_effect": "no_visual_change",
                "surface_transition": "none",
                "surface_continuity": "preserved",
                "decision_mode": "boundary_reconciliation",
                "decision_rule": (
                    "Reconcile existing memory without another GUI verification action. If "
                    "active memory explicitly names an unresolved target, act only toward "
                    "that target; otherwise the next decision must be terminal."
                ),
                "instruction": (
                    "Do not continue in that direction or reverse merely to recheck handled "
                    "records. The unchanged frame proves the same surface remained active; "
                    "do not tap navigation or chrome to reconfirm it because that adds no "
                    "boundary or coverage evidence. Reverse only for a specific unresolved "
                    "target named by active "
                    "memory. Otherwise reconcile coverage and complete when no encountered "
                    "target remains unsatisfied. Runtime does not assert completion."
                ),
            }, ensure_ascii=False)
        )
    if visible_commit_control:
        attempt_contract += (
            "\n\n## Same-frame runtime feedback\n"
            + json.dumps({
                "status": "completion_requires_recheck",
                "visible_commit_controls": [{
                    "label": visible_commit_control,
                    "kind": "button",
                }],
                "instruction": (
                    "Re-evaluate the current state against this frame and the last "
                    "actually recorded action. Completion is the State role's judgment; "
                    "activate the pending requested commit and never claim an activation "
                    "absent from WorkerMemory."
                ),
            }, ensure_ascii=False)
        )
    messages = _actor_messages(
        _actor_report(selected),
        _screenshot(run_dir, frame_no, observation),
        attempt_contract=attempt_contract,
        state=state,
        application_knowledge="\n\n".join(
            value.worker_context() for value in _current_app_knowledge(context)
        ),
        image_scale=float(getattr(model_config, "image_scale", 1.0)),
        profile=spec.profile,
    )
    request_model = getattr(model_config, "model", None)
    if request_model is None:
        request_model = getattr(model, "model_name", None) or getattr(model, "model", None)
    bind_kwargs = chat_request_kwargs(request_model)
    bound, decision_instruction, repair_instruction = bind_actor_decision_transport(
        model,
        tools,
        protocol=action_protocol,
        bind_kwargs=bind_kwargs,
    )
    if decision_instruction:
        messages.append(HumanMessage(content=decision_instruction))
    tools_by_name = {tool["function"]["name"]: tool for tool in tools}
    results = []
    for number in range(1, samples + 1):
        replay_messages, repairs, repair_errors = list(messages), 0, []
        memory_repairs, memory_repair_errors = 0, []
        for decision_attempt in range(3):
            response = bound.invoke(replay_messages)
            try:
                decision = _worker_decision(
                    response, actions, tools_by_name, state,
                    action_protocol=action_protocol,
                )
                break
            except Exception as exc:  # noqa: BLE001 - mirrors one production repair
                if repairs:
                    raise
                repairs += 1
                repair_errors.append(f"{type(exc).__name__}: {exc}")
                replay_messages.extend([response, HumanMessage(content=(
                    f"Protocol repair: {exc}. On this SAME frame, "
                    f"{repair_instruction} Do not emit a state field. "
                    "No action was executed."
                ))])
        errors = _mismatches(expected, decision)
        results.append({
            "sample": number,
            **decision,
            "protocol_repairs": repairs,
            "protocol_repair_errors": repair_errors,
            "memory_repairs": memory_repairs,
            "memory_repair_errors": memory_repair_errors,
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
    parser.add_argument("--visible-commit-control", default="")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()
    if not 1 <= args.samples <= 10:
        parser.error("--samples must be between 1 and 10")
    expected = json.loads(args.expect_json) if args.expect_json else None
    run_dir = args.run_dir.expanduser().resolve()
    if args.master:
        result = replay_master_decision(
            run_dir, samples=args.samples, expectation=expected,
        )
    else:
        result = replay_worker_decision(
            run_dir, frame=args.worker_frame, samples=args.samples, expectation=expected,
            visible_commit_control=args.visible_commit_control,
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
