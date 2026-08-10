"""Coding-Master runtime for dynamically orchestrated agentic Workers."""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Literal

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
from gui_agent.core.tool_agent.orchestrator import (
    MasterCompileError,
    WorkerOrchestrationContext,
    compile_master_program,
    execute_master_program,
)
from gui_agent.core.tool_agent.perception import PerceptionMaterializer, PerceptionMode
from gui_agent.core.tool_agent.protocol import (
    CompleteWorkerArgs,
    capability_parameters,
    FailWorkerArgs,
    ProtocolError,
    RequestActionPatchArgs,
    diagnostic_prompt_reports,
    dynamic_worker_tools,
    dynamic_action_tool,
    exactly_one_tool_call,
    image_message,
    materialize_action_patch,
    parse_json_object,
    response_usage,
    worker_action_floor,
)
from gui_agent.core.tool_agent.sandbox import (
    execute_transform,
    validate_transform_row_fields,
    validate_transform_source,
)

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
    """Run reviewed Master programs over autonomous visual and data Workers."""

    def __init__(
        self,
        *,
        bundle: Any,
        platform: Any,
        log_dir: Path,
        perception_mode: PerceptionMode,
        max_master_programs: int = 3,
        max_compile_attempts: int = 5,
        status_cb: Callable[[str], None] | None = None,
    ) -> None:
        if bundle.platform != "browser":
            raise ValueError("tool-agent experiment currently supports the browser adapter")
        self.bundle = bundle
        self.platform = platform
        self.log_dir = log_dir
        self.perception_mode = perception_mode
        if max_master_programs < 1:
            raise ValueError("max_master_programs must be positive")
        if max_compile_attempts < 1:
            raise ValueError("max_compile_attempts must be positive")
        self.max_master_programs = max_master_programs
        self.max_compile_attempts = max_compile_attempts
        self.data_store = RuntimeDataStore()
        self.master, self.master_cfg = _llm("tool_agent.master")
        self.worker, self.worker_cfg = _llm("tool_agent.worker")
        self.materializer = PerceptionMaterializer(
            mode=perception_mode,
            data_store=self.data_store,
            log_dir=log_dir,
            on_event=self._trace,
        )
        self.trace: list[dict[str, Any]] = []
        self._status_cb = status_cb
        self._started_at = time.perf_counter()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        (self.log_dir / "tool_agent_events.jsonl").write_text("", encoding="utf-8")
        (self.log_dir / "tool_agent.log").write_text("", encoding="utf-8")
        self._frame_no = 0
        self._executor = bundle.make_executor(platform)
        if perception_mode == "vision-only":
            setattr(self._executor, "disable_dom_snap", True)

    def run(self, goal: str, *, knowledge: str = "", page_url: str = "", page_title: str = "") -> ToolAgentRun:
        self._trace(
            "runtime_started",
            goal=goal,
            perception_mode=self.perception_mode,
            master_model=self.master_cfg.model,
            worker_model=self.worker_cfg.model,
        )
        task_context = {
            "goal": goal,
            "page": {"url": page_url, "title": page_title},
            "application_knowledge": knowledge or "(none)",
        }
        final_ref = None
        final_summary = ""
        phase: Literal["completed", "failed"] = "failed"
        orchestration = WorkerOrchestrationContext(
            data_store=self.data_store,
            run_gui_worker=self._run_worker,
            trace=self._trace,
        )
        feedback = ""
        try:
            for generation in range(1, self.max_master_programs + 1):
                program = compile_master_program(
                    llm=self.master,
                    system_prompt=_MASTER_SYSTEM,
                    task_context=task_context,
                    execution_history=orchestration.history_for_model(),
                    feedback=feedback,
                    max_attempts=self.max_compile_attempts,
                    on_event=lambda event, payload: self._trace(
                        event,
                        generation=generation,
                        **payload,
                    ),
                )
                self._trace(
                    "master_program_generated",
                    generation=generation,
                    compile_attempts=program.attempts,
                    source=program.source,
                )
                execution = execute_master_program(program.source, orchestration)
                if execution.error:
                    feedback = (
                        "The reviewed program failed during deterministic execution: "
                        f"{execution.error}. Preserve completed worker IDs and rewrite the entire "
                        "remaining orchestration."
                    )
                    self._trace(
                        "master_program_error",
                        generation=generation,
                        error=execution.error,
                    )
                    continue
                assert execution.terminal is not None
                terminal = execution.terminal
                self._trace(
                    "master_program_completed",
                    generation=generation,
                    phase=terminal.phase,
                    summary=terminal.summary,
                    result_ref=terminal.result_ref,
                )
                if terminal.phase == "completed":
                    final_ref = self.data_store.result_descriptor(terminal.result_ref)
                    phase = "completed"
                    final_summary = terminal.summary
                    break
                if terminal.phase == "failed":
                    final_summary = terminal.summary
                    break
                feedback = (
                    "The previous program explicitly requested replanning: "
                    f"{terminal.summary}. Use the typed Worker history and do not repeat completed work."
                )
                self._trace(
                    "master_replan",
                    generation=generation,
                    reason=terminal.summary,
                )
            else:
                final_summary = "Coding Master exceeded its program/replan limit."
        except KeyboardInterrupt:
            final_summary = "Tool Agent interrupted before reaching a terminal result."
            self._trace(
                "runtime_interrupted",
                summary=final_summary,
            )
        except MasterCompileError as exc:
            final_summary = str(exc)
            self._trace("master_compile_error", error=final_summary)
        except Exception as exc:  # noqa: BLE001 - runtime failure becomes an inspectable result
            final_summary = f"tool-agent runtime failed: {type(exc).__name__}: {exc}"
            self._trace("runtime_error", error=final_summary)

        self._trace(
            "runtime_finished",
            phase=phase,
            summary=final_summary,
            result_ref=final_ref.ref if final_ref is not None else "",
        )
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

    def _run_worker(self, worker_id: str, spec: WorkerSpec) -> WorkerOutcome:
        self._validate_worker_spec(spec)
        self._active_worker_id = worker_id
        self._trace(
            "worker_started",
            worker_id=worker_id,
            profile=spec.profile,
            goal=spec.goal,
            success_criteria=spec.success_criteria,
            requirement_ids=[item.id for item in spec.data_requirements],
            action_names=[item.name for item in spec.actions],
            max_steps=spec.max_steps,
        )
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
                llm_elapsed_s = 0.0
                token_usage: dict[str, int] = {}
                for attempt in range(2):
                    started_at = time.perf_counter()
                    response = self.worker.bind_tools(
                        worker_tools,
                        tool_choice="auto",
                        parallel_tool_calls=False,
                        extra_body={"enable_thinking": True},
                    ).invoke(messages)
                    llm_elapsed_s = time.perf_counter() - started_at
                    token_usage = response_usage(response)
                    try:
                        call = exactly_one_tool_call(response)
                        try:
                            state = WorkerState.model_validate(parse_json_object(response.content))
                        except Exception as state_exc:  # noqa: BLE001 - state-only fallback
                            state = self._recover_worker_state(messages, call, state_exc)
                        break
                    except Exception as exc:  # noqa: BLE001 - one same-frame protocol repair
                        self._trace(
                            "worker_protocol_error",
                            step=step,
                            attempt=attempt + 1,
                            error=str(exc),
                            tool_calls=len(getattr(response, "tool_calls", None) or []),
                            llm_elapsed_s=round(llm_elapsed_s, 3),
                            token_usage=token_usage,
                            context_reports=diagnostic_prompt_reports(
                                "tool_agent.worker.protocol_repair",
                                messages,
                                response,
                                schema="WorkerState + exactly one tool call",
                            ),
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
                    profile=spec.profile,
                    state=state.model_dump(mode="json"),
                    tool=call["name"],
                    args=call["args"],
                    llm_elapsed_s=round(llm_elapsed_s, 3),
                    token_usage=token_usage,
                    context_reports=diagnostic_prompt_reports(
                        "tool_agent.worker",
                        messages[:-1],
                        response,
                        parsed={
                            "state": state.model_dump(mode="json"),
                            "tool_call": call,
                        },
                        schema="WorkerState + exactly one tool call",
                    ),
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
                self._trace(
                    "worker_complete",
                    step=step,
                    profile=spec.profile,
                    result_ref=descriptor.model_dump(mode="json"),
                )
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

    def _recover_worker_state(
        self,
        messages: list[Any],
        call: dict[str, Any],
        state_error: Exception,
    ) -> WorkerState:
        """Recover only the explicit state channel when a valid action exists.

        Some tool-call endpoints omit assistant ``content`` even though they
        return a valid action. Re-running the whole action policy can change the
        decision on the same frame. A state-only call preserves that action and
        restores the independently inspectable state-machine channel.
        """
        prompt = (
            "State-channel repair on the SAME frame. The action policy already selected this "
            "tool call:\n"
            + json.dumps({"name": call["name"], "args": call["args"]}, ensure_ascii=False)
            + "\nReturn only one WorkerState JSON object matching this schema; do not call a tool:\n"
            + json.dumps(WorkerState.model_json_schema(), ensure_ascii=False)
        )
        recovery_model = (
            self.worker.bind(
                max_tokens=2_000,
                extra_body={"enable_thinking": False},
            )
            if callable(getattr(self.worker, "bind", None))
            else self.worker
        )
        started_at = time.perf_counter()
        recovery_messages = [*messages, HumanMessage(content=prompt)]
        response = recovery_model.invoke(recovery_messages)
        llm_elapsed_s = time.perf_counter() - started_at
        state = WorkerState.model_validate(parse_json_object(response.content))
        self._trace(
            "worker_state_recovered",
            tool=call["name"],
            original_error=str(state_error),
            state=state.model_dump(mode="json"),
            llm_elapsed_s=round(llm_elapsed_s, 3),
            token_usage=response_usage(response),
            context_reports=diagnostic_prompt_reports(
                "tool_agent.worker.state_recovery",
                recovery_messages,
                response,
                parsed=state.model_dump(mode="json"),
                schema="WorkerState",
            ),
        )
        return state

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
            screenshot_path=frame.screenshot_path,
            mode=self.perception_mode,
            profile=spec.profile,
            goal=spec.goal,
            chunks=[item.model_dump(mode="json") for item in frame.chunks],
            collections=[item.model_dump(mode="json") for item in frame.collections],
            missing_requirements=frame.missing_requirements,
            requirement_scopes=frame.requirement_scopes,
            applied_filters=frame.applied_filters,
            control_count=len(frame.controls),
        )
        (self.log_dir / f"observation_tool_agent_{self._frame_no}.json").write_text(
            frame.model_dump_json(indent=2),
            encoding="utf-8",
        )
        return frame, png

    def _worker_frame_prompt(self, spec: WorkerSpec, frame: MaterializedFrame) -> str:
        metadata = {
            "profile": spec.profile,
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
            self._trace(
                "runtime_action",
                tool=call["name"],
                profile=spec.profile,
                **payload,
            )
            return payload, None
        if action_spec.capability == "python_transform":
            source = str(full_args.get("source") or "")
            data_ref = str(full_args.get("data_ref") or "")
            collection = self.data_store.collection_descriptor(data_ref)
            scope_status = collection.coverage.get("scope_status")
            if scope_status != "met":
                raise ValueError(
                    f"CollectionRef {data_ref!r} filter scope is {scope_status!r}; "
                    "establish the requested UI scope before collection and transform"
                )
            coverage_status = collection.coverage.get("status")
            if coverage_status != "complete":
                raise ValueError(
                    f"CollectionRef {data_ref!r} coverage is {coverage_status!r}; "
                    "continue observing and traversing before python_transform"
                )
            rows = self.data_store.collection_rows(data_ref)
            value = execute_transform(source, rows, spec.result_schema)
            descriptor = self.data_store.put_result(
                value,
                spec.result_schema,
                summary=f"Worker computed result from {data_ref}.",
            )
            payload = descriptor.model_dump(mode="json")
            self._trace(
                "python_transform",
                tool=call["name"],
                profile=spec.profile,
                data_ref=data_ref,
                result_ref=payload,
            )
            return payload, None
        raise ProtocolError(f"unsupported capability {action_spec.capability!r}")

    @staticmethod
    def _validate_worker_spec(spec: WorkerSpec) -> None:
        Draft202012Validator.check_schema(spec.result_schema)
        for requirement in spec.data_requirements:
            Draft202012Validator.check_schema(requirement.row_schema)
        combined_row_schema = {
            "type": "object",
            "properties": {
                key: value
                for requirement in spec.data_requirements
                for key, value in (requirement.row_schema.get("properties") or {}).items()
            },
        }
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
            validate_transform_row_fields(source, combined_row_schema)

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

    @staticmethod
    def _event_layer(event: str) -> str:
        if event.startswith("master_"):
            return "master"
        if event in {"observe", "perception_extract"}:
            return "observer"
        if event in {"runtime_action"}:
            return "action"
        if event.startswith("data_worker") or event == "python_transform":
            return "data"
        if event.startswith("worker_"):
            return "worker"
        return "runtime"

    @staticmethod
    def _event_message(event: str, payload: dict[str, Any]) -> str:
        if event == "master_compile_attempt":
            issues = list(payload.get("diagnostics") or [])
            return (
                f"Review Master program attempt {payload.get('attempt', '?')}: "
                + (f"{len(issues)} issue(s)" if issues else "passed")
            )
        if event == "master_program_generated":
            return f"Master program generation {payload.get('generation', '?')} is ready"
        if event == "master_worker_dispatch":
            spec = payload.get("spec") if isinstance(payload.get("spec"), dict) else {}
            profile = str(spec.get("profile") or "operator")
            return (
                f"Dispatch {profile} GUI Worker {payload.get('worker_id', '?')}: "
                f"{payload.get('goal', '')}"
            ).strip()
        if event == "worker_started":
            return (
                f"Start {payload.get('profile', 'operator')} Worker "
                f"{payload.get('worker_id', '?')}: {payload.get('goal', '')}"
            )
        if event == "observe":
            collections = []
            for item in payload.get("collections") or []:
                if not isinstance(item, dict):
                    continue
                coverage = item.get("coverage") if isinstance(item.get("coverage"), dict) else {}
                total = coverage.get("known_total")
                count = item.get("row_count", 0)
                amount = f"{count}/{total}" if total is not None else str(count)
                collections.append(
                    f"{item.get('requirement_id', '?')}={amount} {coverage.get('status', 'unknown')}"
                )
            details = ", ".join(collections) or "no collection refs"
            return (
                f"Observe {payload.get('frame_id', '?')} for "
                f"{payload.get('worker_id', '?')}: {details}"
            )
        if event == "perception_extract":
            return (
                f"Visual extraction {payload.get('requirement_id', '?')}: "
                f"{payload.get('row_count', 0)} row(s), "
                f"end_visible={payload.get('end_visible', False)}"
            )
        if event == "worker_decision":
            state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
            summary = str(state.get("summary") or "").strip()
            return (
                f"Worker step {payload.get('step', '?')} [{state.get('status', '?')}] "
                f"→ {payload.get('tool', '?')}"
                + (f": {summary}" if summary else "")
            )
        if event == "runtime_action":
            effect = "no effect" if payload.get("no_effect") else str(payload.get("status") or "")
            return f"{payload.get('action_type', 'GUI')} action via {payload.get('tool', '?')}: {effect}"
        if event == "python_transform":
            result = payload.get("result_ref") if isinstance(payload.get("result_ref"), dict) else {}
            return f"Transform {payload.get('data_ref', '?')} → {result.get('ref', '?')}"
        if event == "worker_complete":
            result = payload.get("result_ref") if isinstance(payload.get("result_ref"), dict) else {}
            return f"Worker completed with {result.get('ref', '?')}"
        if event == "master_worker_result":
            outcome = payload.get("outcome") if isinstance(payload.get("outcome"), dict) else {}
            return f"Worker {payload.get('worker_id', '?')} returned {outcome.get('phase', '?')}"
        if event == "data_worker_start":
            return f"Start Data Worker {payload.get('worker_id', '?')}: {payload.get('goal', '')}"
        if event == "data_worker_complete":
            outcome = payload.get("outcome") if isinstance(payload.get("outcome"), dict) else {}
            return f"Data Worker {payload.get('worker_id', '?')} returned {outcome.get('phase', '?')}"
        if event == "master_program_completed":
            return f"Master program terminal: {payload.get('phase', '?')}"
        if event == "runtime_finished":
            return f"Tool Agent finished: {payload.get('phase', '?')} · {payload.get('summary', '')}"
        return str(
            payload.get("error")
            or payload.get("summary")
            or payload.get("reason")
            or event.replace("_", " ")
        )

    def _human_line(self, entry: dict[str, Any]) -> str:
        event = str(entry.get("event") or "")
        if event == "runtime_started":
            return (
                f"Goal    : {entry.get('goal', '')}\n"
                f"Runtime : Coding Master → Agentic Workers · "
                f"perception={entry.get('perception_mode', '?')}\n"
                f"Models  : master={entry.get('master_model', '?')} · "
                f"worker={entry.get('worker_model', '?')}"
            )
        if event == "master_compile_attempt":
            issues = list(entry.get("diagnostics") or [])
            if issues:
                first_issue = str(issues[0]).splitlines()[0]
                verdict = f"failed · {len(issues)} issue(s) · {first_issue}"
            else:
                verdict = "passed"
            usage = entry.get("token_usage") if isinstance(entry.get("token_usage"), dict) else {}
            metrics = []
            if entry.get("llm_elapsed_s"):
                metrics.append(f"{float(entry['llm_elapsed_s']):.1f}s")
            if usage:
                metrics.append(
                    f"{int(usage.get('input') or 0)}/{int(usage.get('output') or 0)} tok"
                )
            metric_text = f" ({' · '.join(metrics)})" if metrics else ""
            return (
                f"  [Review] g{entry.get('generation', '?')}."
                f"a{entry.get('attempt', '?')} {verdict}{metric_text}"
            )
        if event == "master_program_generated":
            return (
                "Coding Master: reviewed Python ready "
                f"(generation {entry.get('generation', '?')} · "
                f"{entry.get('compile_attempts', '?')} attempts)"
            )
        if event == "master_worker_dispatch":
            spec = entry.get("spec") if isinstance(entry.get("spec"), dict) else {}
            criteria = list(spec.get("success_criteria") or [])
            criteria_text = "\n".join(f"  - {item}" for item in criteria)
            return (
                f"\n--- GUI Worker {entry.get('worker_id', '?')} "
                f"[{spec.get('profile', 'operator')}] ---\n"
                f"Goal    : {entry.get('goal', '')}"
                + (f"\nSuccess :\n{criteria_text}" if criteria_text else "")
            )
        if event == "observe":
            scopes = []
            for requirement_id, scope in (entry.get("requirement_scopes") or {}).items():
                if not isinstance(scope, dict):
                    continue
                requested = scope.get("requested_filters") or {}
                applied = scope.get("applied_filters") or {}
                filters = f" required={requested} applied={applied}" if requested else ""
                scopes.append(f"{requirement_id}:{scope.get('status', 'unknown')}{filters}")
            collections = []
            for item in entry.get("collections") or []:
                if not isinstance(item, dict):
                    continue
                coverage = item.get("coverage") if isinstance(item.get("coverage"), dict) else {}
                total = coverage.get("known_total")
                amount = f"{item.get('row_count', 0)}/{total}" if total is not None else str(item.get("row_count", 0))
                collections.append(
                    f"{item.get('requirement_id', '?')}:{amount} {coverage.get('status', 'unknown')}"
                )
            scope_text = "; ".join(scopes) or "—"
            collection_text = "; ".join(collections) or "waiting"
            frame_id = str(entry.get("frame_id") or "?")
            turn_no = frame_id.rsplit(":", 1)[-1]
            screenshot = str(entry.get("screenshot_path") or "")
            return (
                f"\n--- Turn {turn_no} ---\n"
                + (f"Screenshot : {screenshot}\n" if screenshot else "")
                + f"Observation: {entry.get('mode', '?')} perception · "
                f"{entry.get('control_count', 0)} controls\n"
                f"Scope      : {scope_text}\n"
                f"Collection : {collection_text}"
            )
        if event == "worker_decision":
            state = entry.get("state") if isinstance(entry.get("state"), dict) else {}
            recent: list[dict[str, Any]] = []
            for prior in reversed(self.trace[:-1]):
                if prior.get("event") == "worker_decision":
                    break
                recent.append(prior)
            recent.append(entry)
            timings: dict[str, float] = {}
            input_tokens = 0
            output_tokens = 0
            for item in recent:
                name = str(item.get("event") or "")
                label = {
                    "perception_extract": "perception",
                    "worker_state_recovered": "state",
                    "worker_decision": "policy",
                }.get(name)
                if label and item.get("llm_elapsed_s"):
                    timings[label] = timings.get(label, 0.0) + float(item["llm_elapsed_s"])
                if label:
                    usage = item.get("token_usage") if isinstance(item.get("token_usage"), dict) else {}
                    input_tokens += int(usage.get("input") or 0)
                    output_tokens += int(usage.get("output") or 0)
            timing_text = " | ".join(f"{key}={value:.1f}s" for key, value in timings.items())
            metrics = ""
            if timing_text:
                metrics += f"\nTiming     : {timing_text}"
            if input_tokens or output_tokens:
                metrics += f"\nTokens     : {input_tokens}/{output_tokens}"
            return (
                f"State      : {state.get('status', '?')} · {state.get('summary', '')}\n"
                f"Action plan: {entry.get('tool', '?')} · "
                f"{state.get('next_instruction', '')}"
                f"{metrics}"
            )
        if event == "runtime_action":
            effect = (
                "executed · effect unconfirmed"
                if entry.get("no_effect") and entry.get("status") == "executed"
                else entry.get("status", "")
            )
            settle = float(entry.get("settle_seconds") or 0)
            return (
                f"Action     : {entry.get('action_type', '?')} via {entry.get('tool', '?')}\n"
                f"Result     : {effect}"
                + (f" · settle={settle:.1f}s" if settle else "")
            )
        if event == "worker_tool_error":
            error = str(entry.get("error") or "").splitlines()[0]
            return f"Result     : ERROR · {error}"
        if event == "worker_action_patch":
            action = entry.get("action") if isinstance(entry.get("action"), dict) else {}
            return (
                f"Action set : +{action.get('name', '?')} "
                f"({action.get('capability', '?')})"
            )
        if event == "python_transform":
            result = entry.get("result_ref") if isinstance(entry.get("result_ref"), dict) else {}
            return f"Result     : {entry.get('data_ref', '?')} → {result.get('ref', '?')}"
        if event == "worker_complete":
            result = entry.get("result_ref") if isinstance(entry.get("result_ref"), dict) else {}
            return f"Verification: completed · {result.get('ref', '?')}"
        if event == "master_worker_result":
            outcome = entry.get("outcome") if isinstance(entry.get("outcome"), dict) else {}
            return f"Worker outcome: {entry.get('worker_id', '?')} · {outcome.get('phase', '?')}"
        if event == "master_replan":
            return f"MASTER  replan · {entry.get('reason', '')}"
        if event == "data_worker_start":
            return (
                f"\n--- Data Worker {entry.get('worker_id', '?')} ---\n"
                f"Goal    : {entry.get('goal', '')}\n"
                f"Inputs  : {entry.get('inputs', [])}"
            )
        if event == "data_worker_complete":
            outcome = entry.get("outcome") if isinstance(entry.get("outcome"), dict) else {}
            result = outcome.get("result_ref") if isinstance(outcome.get("result_ref"), dict) else {}
            return (
                f"Result  : {outcome.get('phase', '?')}"
                + (f" · {result.get('ref')}" if result.get("ref") else "")
            )
        if event in {
            "master_compile_error",
            "master_program_error",
            "runtime_error",
            "runtime_interrupted",
        }:
            return f"ERROR   {entry.get('message', '')}"
        if event == "runtime_finished":
            return (
                "\n--- Final Result ---\n"
                f"Status  : {entry.get('phase', '?')}\n"
                f"Summary : {entry.get('summary', '')}\n"
                f"ResultRef: {entry.get('result_ref', '') or '—'}\n"
                f"Elapsed : {float(entry.get('elapsed_s') or 0):.1f}s"
            )
        return ""

    def _trace(self, event: str, **payload: Any) -> None:
        trace = getattr(self, "trace", None)
        if trace is None:
            self.trace = []
            trace = self.trace
        layer = self._event_layer(event)
        message = self._event_message(event, payload)
        started_at = getattr(self, "_started_at", None)
        elapsed_s = round(time.perf_counter() - started_at, 3) if started_at is not None else 0.0
        entry = {
            "index": len(trace) + 1,
            "timestamp": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "elapsed_s": elapsed_s,
            "layer": layer,
            "event": event,
            "message": message,
            **payload,
        }
        if (
            "worker_id" not in entry
            and getattr(self, "_active_worker_id", "")
            and (layer in {"worker", "observer", "action"} or event == "python_transform")
        ):
            entry["worker_id"] = self._active_worker_id
        trace.append(entry)

        log_dir = getattr(self, "log_dir", None)
        if isinstance(log_dir, Path):
            log_dir.mkdir(parents=True, exist_ok=True)
            with (log_dir / "tool_agent_events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
            human_line = self._human_line(entry)
            if human_line:
                with (log_dir / "tool_agent.log").open("a", encoding="utf-8") as stream:
                    stream.write(human_line + "\n")
                print(human_line)
            self._write_live_artifacts(
                write_data=event in {
                    "observe",
                    "python_transform",
                    "data_worker_complete",
                    "runtime_interrupted",
                }
            )

        status_cb = getattr(self, "_status_cb", None)
        if status_cb is not None and self._human_line(entry):
            try:
                status_cb(f"{layer.title()} · {message}")
            except Exception:
                pass

    def _write_live_artifacts(self, *, write_data: bool = False) -> None:
        """Keep crash/interruption-readable snapshots current after every event."""
        raw = {
            "phase": "running",
            "summary": self.trace[-1].get("message", "") if self.trace else "",
            "trace": self.trace,
            "master_model": getattr(getattr(self, "master_cfg", None), "model", ""),
            "worker_model": getattr(getattr(self, "worker_cfg", None), "model", ""),
            "perception_model": getattr(getattr(self, "materializer", None), "model", ""),
            "perception_mode": getattr(self, "perception_mode", "enhanced"),
        }
        target = self.log_dir / "tool_agent_trace.json"
        temporary = self.log_dir / "tool_agent_trace.json.tmp"
        temporary.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(target)
        data_store = getattr(self, "data_store", None)
        if write_data and data_store is not None:
            (self.log_dir / "tool_agent_data_store.json").write_text(
                json.dumps(data_store.private_dump(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

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
