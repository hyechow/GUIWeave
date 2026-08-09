"""Master/Worker runtime for dynamic tool-call GUI execution."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from jsonschema import Draft202012Validator, validate
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI

from gui_agent.core.config import resolve_llm_config
from gui_agent.prompts import load_prompt_text
from gui_agent.core.run.action_exec import settle_after_action
from gui_agent.core.schemas import BaseAction, BaseActionDecision
from gui_agent.core.tool_agent.contracts import (
    DynamicActionSpec,
    MaterializedFrame,
    ToolAgentRun,
    WorkerOutcome,
    WorkerSpec,
    WorkerState,
)
from gui_agent.core.tool_agent.data_store import RuntimeDataStore
from gui_agent.core.tool_agent.perception import PerceptionMaterializer, PerceptionMode
from gui_agent.core.tool_agent.protocol import (
    CompleteWorkerArgs,
    capability_parameters,
    FailTaskArgs,
    FailWorkerArgs,
    FinishTaskArgs,
    ProtocolError,
    RequestActionPatchArgs,
    RunWorkerArgs,
    dynamic_worker_tools,
    dynamic_action_tool,
    exactly_one_tool_call,
    image_message,
    materialize_action_patch,
    master_tools,
    parse_json_object,
    worker_action_floor,
)
from gui_agent.core.tool_agent.sandbox import execute_transform, validate_transform_source

_MASTER_SYSTEM = load_prompt_text("task.tool_agent.master")
_WORKER_SYSTEM = load_prompt_text("task.tool_agent.worker")
_MAX_ACTION_PATCHES_PER_FRAME = 3
_RUNTIME_WORKER_TOOL_NAMES = {"request_action_patch", "complete", "fail"}


def _llm(config_name: str, *, temperature: float = 0) -> tuple[ChatOpenAI, Any]:
    cfg = resolve_llm_config(config_name)
    return (
        ChatOpenAI(
            model=cfg.model,
            api_key=cfg.api_key,
            base_url=cfg.base_url,
            timeout=cfg.timeout_s,
            max_retries=cfg.max_retries,
            temperature=temperature,
        ),
        cfg,
    )


class ToolAgentRuntime:
    """Run a text Master and one or more dynamically-specified visual Workers."""

    def __init__(
        self,
        *,
        bundle: Any,
        platform: Any,
        log_dir: Path,
        perception_mode: PerceptionMode,
        max_master_steps: int = 6,
    ) -> None:
        if bundle.platform != "browser":
            raise ValueError("tool-agent experiment currently supports the browser adapter")
        self.bundle = bundle
        self.platform = platform
        self.log_dir = log_dir
        self.perception_mode = perception_mode
        self.max_master_steps = max_master_steps
        self.data_store = RuntimeDataStore()
        self.master, self.master_cfg = _llm("tool_agent.master")
        self.worker, self.worker_cfg = _llm("tool_agent.worker")
        self.materializer = PerceptionMaterializer(
            mode=perception_mode,
            data_store=self.data_store,
            log_dir=log_dir,
        )
        self.trace: list[dict[str, Any]] = []
        self._frame_no = 0
        self._executor = bundle.make_executor(platform)
        if perception_mode == "vision-only":
            setattr(self._executor, "disable_dom_snap", True)

    def run(self, goal: str, *, knowledge: str = "", page_url: str = "", page_title: str = "") -> ToolAgentRun:
        task_context = {
            "goal": goal,
            "page": {"url": page_url, "title": page_title},
            "application_knowledge": knowledge or "(none)",
        }
        messages: list[Any] = [
            SystemMessage(content=_MASTER_SYSTEM),
            HumanMessage(content=json.dumps(task_context, ensure_ascii=False)),
        ]
        final_ref = None
        final_summary = ""
        phase: Literal["completed", "failed"] = "failed"
        try:
            for step in range(1, self.max_master_steps + 1):
                response = self._invoke_master(messages)
                call = exactly_one_tool_call(response)
                self._trace("master_tool", step=step, tool=call["name"], args=call["args"])
                messages.append(response)
                if call["name"] == "run_worker":
                    try:
                        parsed = RunWorkerArgs.model_validate(call["args"])
                        outcome = self._run_worker(parsed.spec)
                        payload = outcome.model_dump(mode="json")
                    except Exception as exc:  # noqa: BLE001 - Master may repair its WorkerSpec
                        payload = {
                            "phase": "failed",
                            "summary": f"WorkerSpec/runtime error: {type(exc).__name__}: {exc}",
                            "steps": 0,
                        }
                        self._trace("worker_spec_error", step=step, error=payload["summary"])
                    messages.append(ToolMessage(
                        content=json.dumps(payload, ensure_ascii=False),
                        tool_call_id=call["id"],
                    ))
                    continue
                if call["name"] == "finish_task":
                    parsed = FinishTaskArgs.model_validate(call["args"])
                    final_ref = self.data_store.result_descriptor(parsed.result_ref)
                    phase = "completed"
                    final_summary = final_ref.summary or "Master accepted the worker ResultRef."
                    messages.append(ToolMessage(content="accepted", tool_call_id=call["id"]))
                    break
                if call["name"] == "fail_task":
                    parsed = FailTaskArgs.model_validate(call["args"])
                    final_summary = parsed.reason
                    messages.append(ToolMessage(content="stopped", tool_call_id=call["id"]))
                    break
                raise ProtocolError(f"unknown Master tool {call['name']!r}")
            else:
                final_summary = "Master exceeded its tool-call step limit."
        except Exception as exc:  # noqa: BLE001 - runtime failure becomes an inspectable result
            final_summary = f"tool-agent runtime failed: {type(exc).__name__}: {exc}"
            self._trace("runtime_error", error=final_summary)

        output = self.data_store.result_value(final_ref.ref) if final_ref is not None else None
        run = ToolAgentRun(
            phase=phase,
            summary=final_summary,
            output=output,
            result_ref=final_ref,
            trace=self.trace,
            master_model=self.master_cfg.model,
            worker_model=self.worker_cfg.model,
            perception_model=self.materializer.model,
            perception_mode=self.perception_mode,
        )
        self._write_artifacts(run)
        return run

    def _invoke_master(self, messages: list[Any]) -> Any:
        try:
            return self.master.bind_tools(
                master_tools(),
                tool_choice="required",
                parallel_tool_calls=False,
                extra_body={"enable_thinking": False},
            ).invoke(messages)
        except Exception as exc:
            raise RuntimeError(
                "qwen3.7-max Master tool-call compatibility failure; no fallback was used: "
                f"{exc}"
            ) from exc

    def _run_worker(self, spec: WorkerSpec) -> WorkerOutcome:
        self._validate_worker_spec(spec)
        active_actions = self._initial_worker_actions(spec)
        worker_tools = dynamic_worker_tools(active_actions)
        spec_for_prompt = spec.model_dump(mode="json")
        spec_for_prompt["actions"] = [
            action.model_dump(mode="json") for action in active_actions
        ]
        for action in spec_for_prompt["actions"]:
            if action["capability"] == "python_transform":
                source = str(action["fixed_args"].pop("source", ""))
                action["fixed_args"]["source_bound"] = bool(source)
        messages: list[Any] = [
            SystemMessage(
                content=(
                    _WORKER_SYSTEM
                    + "\n\nWorkerState JSON Schema:\n"
                    + json.dumps(WorkerState.model_json_schema(), ensure_ascii=False)
                    + "\n\nWorkerSpec (transform source is bound but hidden):\n"
                    + json.dumps(spec_for_prompt, ensure_ascii=False)
                )
            )
        ]
        last_result_ref = None
        for step in range(1, spec.max_steps + 1):
            frame, png = self._observe(spec)
            prompt = self._worker_frame_prompt(spec, frame)
            frame_message = image_message(prompt, png)
            messages.append(frame_message)
            patch_turn = 0
            while True:
                response = None
                state = None
                call = None
                for attempt in range(2):
                    response = self.worker.bind_tools(
                        worker_tools,
                        tool_choice="auto",
                        parallel_tool_calls=False,
                        extra_body={"enable_thinking": True},
                    ).invoke(messages)
                    try:
                        state = WorkerState.model_validate(parse_json_object(response.content))
                        call = exactly_one_tool_call(response)
                        break
                    except Exception as exc:  # noqa: BLE001 - one same-frame protocol repair
                        self._trace(
                            "worker_protocol_error",
                            step=step,
                            attempt=attempt + 1,
                            error=str(exc),
                        )
                        if attempt:
                            raise
                        messages.append(response)
                        messages.append(HumanMessage(content=(
                            "Protocol repair: the previous response was invalid. On this SAME frame, "
                            "emit WorkerState JSON in content and exactly one tool call. No action was executed."
                        )))
                assert response is not None and state is not None and call is not None
                messages.append(response)
                self._trace(
                    "worker_decision",
                    step=step,
                    patch_turn=patch_turn,
                    frame_id=frame.frame_id,
                    state=state.model_dump(mode="json"),
                    tool=call["name"],
                    args=call["args"],
                )
                if call["name"] != "request_action_patch":
                    break
                patch_turn += 1
                try:
                    if patch_turn > _MAX_ACTION_PATCHES_PER_FRAME:
                        raise ProtocolError("same-frame action patch limit exceeded")
                    parsed_patch = RequestActionPatchArgs.model_validate(call["args"])
                    added_action = materialize_action_patch(parsed_patch)
                    self._validate_action_spec(added_action)
                    existing = next(
                        (item for item in active_actions if item.name == added_action.name),
                        None,
                    )
                    if existing is not None:
                        if existing != added_action:
                            raise ProtocolError(
                                f"action name {added_action.name!r} already has a different contract"
                            )
                        patch_payload = {
                            "status": "already_available",
                            "action": existing.model_dump(mode="json"),
                        }
                    else:
                        active_actions.append(added_action)
                        worker_tools = dynamic_worker_tools(active_actions)
                        patch_payload = {
                            "status": "added",
                            "action": added_action.model_dump(mode="json"),
                            "instruction": (
                                "The action is now available. Reason again and choose exactly one "
                                "action on the same screenshot."
                            ),
                        }
                        self._trace(
                            "worker_action_patch",
                            step=step,
                            frame_id=frame.frame_id,
                            reason=parsed_patch.reason,
                            action=added_action.model_dump(mode="json"),
                        )
                except Exception as exc:  # noqa: BLE001 - Worker may repair its patch request
                    patch_payload = {
                        "status": "error",
                        "error": f"{type(exc).__name__}: {exc}",
                        "instruction": (
                            "Request a valid registered GUI capability or use an already available "
                            "action on this same screenshot."
                        ),
                    }
                    self._trace(
                        "worker_action_patch_error",
                        step=step,
                        frame_id=frame.frame_id,
                        error=patch_payload["error"],
                    )
                messages.append(ToolMessage(
                    content=json.dumps(patch_payload, ensure_ascii=False),
                    tool_call_id=call["id"],
                ))
                if patch_turn > _MAX_ACTION_PATCHES_PER_FRAME:
                    return WorkerOutcome(
                        phase="failed",
                        summary="Worker exceeded the same-frame action patch limit.",
                        steps=step - 1,
                    )
            try:
                result_payload, terminal = self._execute_worker_tool(
                    spec,
                    active_actions,
                    call,
                    png,
                )
            except Exception as exc:  # noqa: BLE001 - feed capability failure back into ReAct
                result_payload = {
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                    "recovery": (
                        "If this is a bound python_transform error, call fail so the Master can "
                        "replace the WorkerSpec. Do not repeat the same call."
                    ),
                }
                terminal = None
                self._trace("worker_tool_error", step=step, tool=call["name"], error=result_payload["error"])
            if isinstance(result_payload, dict) and result_payload.get("kind") == "result":
                last_result_ref = result_payload["ref"]
            messages.append(ToolMessage(
                content=json.dumps(result_payload, ensure_ascii=False),
                tool_call_id=call["id"],
            ))
            if terminal == "complete":
                parsed = CompleteWorkerArgs.model_validate(call["args"])
                descriptor = self.data_store.result_descriptor(parsed.result_ref)
                self._trace("worker_complete", step=step, result_ref=descriptor.model_dump(mode="json"))
                return WorkerOutcome(
                    phase="completed",
                    summary=state.summary,
                    result_ref=descriptor,
                    steps=step,
                )
            if terminal == "fail":
                reason = FailWorkerArgs.model_validate(call["args"]).reason
                return WorkerOutcome(phase="failed", summary=reason, steps=step)
        return WorkerOutcome(
            phase="failed",
            summary=(
                f"Worker exceeded {spec.max_steps} steps"
                + (f" after producing {last_result_ref}" if last_result_ref else "")
            ),
            steps=spec.max_steps,
        )

    def _observe(self, spec: WorkerSpec) -> tuple[MaterializedFrame, bytes]:
        self._frame_no += 1
        frame, png = self.materializer.observe(
            bundle=self.bundle,
            platform=self.platform,
            requirements=spec.data_requirements,
            frame_no=self._frame_no,
        )
        self._trace(
            "observe",
            frame_id=frame.frame_id,
            mode=self.perception_mode,
            chunks=[item.model_dump(mode="json") for item in frame.chunks],
            collections=[item.model_dump(mode="json") for item in frame.collections],
            missing_requirements=frame.missing_requirements,
        )
        return frame, png

    def _worker_frame_prompt(self, spec: WorkerSpec, frame: MaterializedFrame) -> str:
        metadata = {
            "worker_goal": spec.goal,
            "success_criteria": spec.success_criteria,
            "frame": frame.model_dump(mode="json", exclude={"screenshot_path"}),
        }
        return (
            "Current frame metadata and runtime artifacts follow. Values are intentionally absent.\n"
            + json.dumps(metadata, ensure_ascii=False)
        )

    def _execute_worker_tool(
        self,
        spec: WorkerSpec,
        actions: list[DynamicActionSpec],
        call: dict[str, Any],
        png: bytes,
    ) -> tuple[dict[str, Any], str | None]:
        if call["name"] == "complete":
            parsed = CompleteWorkerArgs.model_validate(call["args"])
            descriptor = self.data_store.result_descriptor(parsed.result_ref)
            return descriptor.model_dump(mode="json"), "complete"
        if call["name"] == "fail":
            parsed = FailWorkerArgs.model_validate(call["args"])
            return {"status": "failed", "reason": parsed.reason}, "fail"
        action_by_name = {item.name: item for item in actions}
        action_spec = action_by_name.get(call["name"])
        if action_spec is None:
            raise ProtocolError(f"unknown Worker tool {call['name']!r}")
        full_args = {**action_spec.fixed_args, **call["args"]}
        parameters = dynamic_action_tool(action_spec)["function"]["parameters"]
        validate(instance=call["args"], schema=parameters)
        if action_spec.capability in {"tap", "scroll", "select_option"}:
            for coordinate in ("x", "y"):
                value = full_args.get(coordinate)
                if value is not None and not 0 <= float(value) < 1000:
                    raise ValueError(f"{action_spec.name}: {coordinate} must be in [0, 1000)")
            action = BaseAction.model_validate(
                {
                    "action_type": action_spec.capability,
                    "description": full_args.pop("description", action_spec.description),
                    **full_args,
                }
            )
            decision = BaseActionDecision(action=action)
            executed = self._executor.execute(decision, png_bytes=png)
            elapsed, no_effect = settle_after_action(
                self.platform,
                png,
                action_type=action.action_type,
            )
            payload = {
                "status": "executed" if executed else "failed",
                "action_type": action.action_type,
                "settle_seconds": round(elapsed, 3),
                "no_effect": no_effect,
            }
            self._trace("runtime_action", tool=call["name"], **payload)
            return payload, None
        if action_spec.capability == "python_transform":
            source = str(full_args.get("source") or "")
            data_ref = str(full_args.get("data_ref") or "")
            rows = self.data_store.collection_rows(data_ref)
            value = execute_transform(source, rows, spec.result_schema)
            descriptor = self.data_store.put_result(
                value,
                spec.result_schema,
                summary=f"Worker computed result from {data_ref}.",
            )
            payload = descriptor.model_dump(mode="json")
            self._trace("python_transform", tool=call["name"], data_ref=data_ref, result_ref=payload)
            return payload, None
        raise ProtocolError(f"unsupported capability {action_spec.capability!r}")

    @staticmethod
    def _validate_worker_spec(spec: WorkerSpec) -> None:
        Draft202012Validator.check_schema(spec.result_schema)
        for requirement in spec.data_requirements:
            Draft202012Validator.check_schema(requirement.row_schema)
        for action in spec.actions:
            ToolAgentRuntime._validate_action_spec(action)
        for action in spec.actions:
            if action.capability != "python_transform":
                continue
            source = str(action.fixed_args.get("source") or "")
            if not source.strip():
                raise ValueError(f"{action.name}: python_transform requires fixed_args.source")
            if "data_ref" not in action.exposed_args:
                raise ValueError(f"{action.name}: python_transform must expose data_ref")
            validate_transform_source(source)

    @staticmethod
    def _validate_action_spec(action: DynamicActionSpec) -> None:
        parameters = capability_parameters(action.capability)
        properties = parameters.get("properties") or {}
        allowed_extra = {"source"} if action.capability == "python_transform" else set()
        unknown_fixed = set(action.fixed_args).difference(properties).difference(allowed_extra)
        if unknown_fixed:
            raise ValueError(f"{action.name}: unknown fixed args {sorted(unknown_fixed)}")
        for name, value in action.fixed_args.items():
            if name in properties:
                validate(instance=value, schema=properties[name])
        missing_required = (
            set(parameters.get("required") or [])
            .difference(action.fixed_args)
            .difference(action.exposed_args)
        )
        if missing_required:
            raise ValueError(
                f"{action.name}: required args are neither fixed nor exposed: "
                f"{sorted(missing_required)}"
            )
        dynamic_action_tool(action)

    @staticmethod
    def _initial_worker_actions(spec: WorkerSpec) -> list[DynamicActionSpec]:
        floor = worker_action_floor()
        reserved = _RUNTIME_WORKER_TOOL_NAMES.union(item.name for item in floor)
        collisions = reserved.intersection(item.name for item in spec.actions)
        if collisions:
            raise ValueError(f"WorkerSpec uses reserved runtime action names: {sorted(collisions)}")
        return [*spec.actions, *floor]

    def _trace(self, event: str, **payload: Any) -> None:
        self.trace.append({"index": len(self.trace) + 1, "event": event, **payload})

    def _write_artifacts(self, run: ToolAgentRun) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        (self.log_dir / "tool_agent_trace.json").write_text(
            run.model_dump_json(indent=2), encoding="utf-8"
        )
        (self.log_dir / "tool_agent_data_store.json").write_text(
            json.dumps(self.data_store.private_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


__all__ = ["ToolAgentRuntime", "ToolAgentRun"]
