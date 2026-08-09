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
    RunWorkerArgs,
    dynamic_worker_tools,
    dynamic_action_tool,
    exactly_one_tool_call,
    image_message,
    master_tools,
    parse_json_object,
)
from gui_agent.core.tool_agent.sandbox import execute_transform, validate_transform_source

_MASTER_SYSTEM = load_prompt_text("task.tool_agent.master")
_WORKER_SYSTEM = load_prompt_text("task.tool_agent.worker")


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
        worker_tools = dynamic_worker_tools(spec)
        spec_for_prompt = spec.model_dump(mode="json")
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
                frame_id=frame.frame_id,
                state=state.model_dump(mode="json"),
                tool=call["name"],
                args=call["args"],
            )
            try:
                result_payload, terminal = self._execute_worker_tool(spec, call, png)
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
        actions = {item.name: item for item in spec.actions}
        action_spec = actions.get(call["name"])
        if action_spec is None:
            raise ProtocolError(f"unknown Worker tool {call['name']!r}")
        full_args = {**action_spec.fixed_args, **call["args"]}
        parameters = dynamic_action_tool(action_spec)["function"]["parameters"]
        validate(instance=call["args"], schema=parameters)
        if action_spec.capability in {"tap", "scroll"}:
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
        transforms = [item for item in spec.actions if item.capability == "python_transform"]
        for action in spec.actions:
            parameters = capability_parameters(action.capability)
            properties = parameters.get("properties") or {}
            allowed_extra = {"source"} if action.capability == "python_transform" else set()
            unknown_fixed = set(action.fixed_args).difference(properties).difference(allowed_extra)
            if unknown_fixed:
                raise ValueError(f"{action.name}: unknown fixed args {sorted(unknown_fixed)}")
            for name, value in action.fixed_args.items():
                if name in properties:
                    validate(instance=value, schema=properties[name])
            dynamic_action_tool(action)
        for action in transforms:
            source = str(action.fixed_args.get("source") or "")
            if not source.strip():
                raise ValueError(f"{action.name}: python_transform requires fixed_args.source")
            if "data_ref" not in action.exposed_args:
                raise ValueError(f"{action.name}: python_transform must expose data_ref")
            validate_transform_source(source)

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
