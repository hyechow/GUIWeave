"""Coding-Master runtime for dynamically orchestrated agentic Workers."""

from __future__ import annotations

import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from math import hypot
from pathlib import Path
from typing import Any, Callable, Literal
from urllib.parse import unquote, urlsplit

from jsonschema import Draft202012Validator, validate
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

from gui_agent.core.config import resolve_llm_config
from gui_agent.prompts import load_prompt_text
from gui_agent.core.runtime.action_settle import (
    VERIFY_TIMEOUT_S,
    has_snapped_point,
    settle_after_action,
)
from gui_agent.core.schemas import BaseAction, BaseActionDecision, TargetVerify
from gui_agent.core.tool_agent.contracts import (
    CollectionRef,
    DynamicActionSpec,
    MaterializedFrame,
    ToolAgentRun,
    WorkerOutcome,
    WorkerSpec,
    WorkerState,
)
from gui_agent.core.tool_agent.action_guard import (
    WorkerActionCircuitBreaker,
    control_at_point,
    is_candidate_commit,
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
    CompleteReadyWorkerArgs,
    FailWorkerArgs,
    MAX_ORDERED_ACTIONS,
    ProtocolError,
    RequestActionPatchArgs,
    cacheable_system_message,
    capability_parameters,
    diagnostic_prompt_reports,
    dynamic_worker_tools,
    dynamic_action_tool,
    exactly_one_tool_call,
    image_message,
    materialize_action_patch,
    normalize_action_arguments,
    parse_json_object,
    response_usage,
    validate_dynamic_action_spec,
    worker_action_floor,
)
from gui_agent.core.tool_agent.replay import write_replay_artifact
from gui_agent.core.tool_agent.worker_memory import (
    WorkerJournal,
    build_worker_memory_view,
    project_worker_context,
)
from gui_agent.core.vision.target_verify import verify_target

_MASTER_SYSTEM = load_prompt_text("task.tool_agent.master")
_MASTER_REDELEGATE_SYSTEM = load_prompt_text("task.tool_agent.master_redelegate")
_WORKER_SYSTEM = load_prompt_text("task.tool_agent.worker")
_MAX_ACTION_PATCHES_PER_FRAME = 3
_MAX_ACTION_GUARD_REPAIRS_PER_FRAME = 1
_MAX_LOGICAL_WORKER_STEPS = 40
_LOCAL_REPLAN_EXTRA_STEPS = 28
_MULTI_ACTION_TARGET_TOLERANCE = 80.0
_RUNTIME_WORKER_TOOL_NAMES = {
    "continue_with_actions",
    "request_action_patch",
    "complete",
    "fail",
}
_SPATIAL_CAPABILITIES = {
    "tap", "type", "scroll", "drag", "long_press", "select_option",
}
_EXECUTABLE_CAPABILITIES = {
    *_SPATIAL_CAPABILITIES,
    "clear_text",
    "press_enter",
    "open_url",
    "back",
    "home",
    "app_switch",
    "launch_app",
}


class _RuntimeCancelled(Exception):
    """Internal cooperative stop raised only at safe runtime boundaries."""
_ACTION_TYPES = {"open_url": "navigate"}
_REDACTED_ACCESS_VALUE = "[session access value redacted]"
_WORKER_VERIFY_POOL = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="tool-worker-verify",
)
_TARGET_VERIFIED_ACTION_TYPES = {
    "tap", "click", "type", "long_press", "select_option",
}


def _token_metric(
    input_tokens: int,
    output_tokens: int,
    cached_input: int = 0,
) -> str:
    """Render gross token usage plus provider-reported prompt-cache reuse."""

    input_tokens = max(0, int(input_tokens))
    output_tokens = max(0, int(output_tokens))
    cached_input = max(0, min(input_tokens, int(cached_input)))
    text = f"{input_tokens}/{output_tokens} tok"
    if cached_input and input_tokens:
        text += f" · cache {cached_input} ({cached_input / input_tokens:.0%})"
    return text


def _supports_explicit_prompt_cache(config: Any) -> bool:
    return (
        str(getattr(config, "provider", "")).casefold() in {"dashscope", "tokenplan"}
        and str(getattr(config, "model", "")).casefold().startswith("qwen")
    )


def _access_log_redactions(access_context: str) -> tuple[str, ...]:
    """Return exact sensitive strings that must never reach durable run artifacts."""

    values = {access_context.strip()} if access_context.strip() else set()
    # Deployment knowledge conventionally wraps account names, passwords and tokens
    # in Markdown code spans.  Keep the whole block private and also redact individual
    # values in case a Worker emits one as a tool argument or state summary.
    values.update(
        match.strip()
        for match in re.findall(r"`([^`\n]+)`", access_context)
        if match.strip()
    )
    return tuple(sorted(values, key=len, reverse=True))


def _redact_log_value(value: Any, redactions: tuple[str, ...]) -> Any:
    """Recursively copy a report payload while replacing session access values."""

    if not redactions:
        return value
    if isinstance(value, str):
        for secret in redactions:
            value = value.replace(secret, _REDACTED_ACCESS_VALUE)
        return value
    if isinstance(value, dict):
        return {
            key: _redact_log_value(item, redactions)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_log_value(item, redactions) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_log_value(item, redactions) for item in value)
    return value


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
    """Run one reviewed Master program over autonomous visual Workers."""

    def __init__(
        self,
        *,
        bundle: Any,
        platform: Any,
        log_dir: Path,
        perception_mode: PerceptionMode,
        max_turns: int = 50,
        max_subgoal_replans: int = 2,
        max_compile_attempts: int = 5,
        allow_multi_action: bool = False,
        status_cb: Callable[[str], None] | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ) -> None:
        if max_turns < 1:
            raise ValueError("max_turns must be positive")
        if max_turns > 50:
            raise ValueError("max_turns cannot exceed 50")
        platform_capabilities = tuple(
            getattr(bundle, "tool_agent_capabilities", ()) or ()
        )
        if not platform_capabilities:
            raise ValueError(
                f"tool-agent runtime is not enabled for the {bundle.platform} adapter"
            )
        self.bundle = bundle
        self.platform = platform
        self._platform_capabilities = frozenset(platform_capabilities)
        self.log_dir = log_dir
        self.perception_mode = perception_mode
        if max_subgoal_replans < 0:
            raise ValueError("max_subgoal_replans cannot be negative")
        if max_compile_attempts < 1:
            raise ValueError("max_compile_attempts must be positive")
        self.max_turns = max_turns
        self.max_subgoal_replans = max_subgoal_replans
        self.max_compile_attempts = max_compile_attempts
        self.allow_multi_action = allow_multi_action
        self.data_store = RuntimeDataStore()
        self.master, self.master_cfg = _llm("tool_agent.master")
        self.worker, self.worker_cfg = _llm("tool_agent.worker")
        self._master_explicit_cache = _supports_explicit_prompt_cache(self.master_cfg)
        self._worker_explicit_cache = _supports_explicit_prompt_cache(self.worker_cfg)
        self.materializer = PerceptionMaterializer(
            mode=perception_mode,
            data_store=self.data_store,
            log_dir=log_dir,
            on_event=self._trace,
        )
        self.trace: list[dict[str, Any]] = []
        self._status_cb = status_cb
        self._stop_requested = stop_requested
        self._started_at = time.perf_counter()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        (self.log_dir / "tool_agent_events.jsonl").write_text("", encoding="utf-8")
        (self.log_dir / "tool_agent.log").write_text("", encoding="utf-8")
        self._frame_no = 0
        self._worker_access_context = ""
        self._access_log_redactions: tuple[str, ...] = ()
        self._worker_journals: dict[str, WorkerJournal] = {}
        self._worker_last_frames: dict[str, MaterializedFrame] = {}
        self._worker_platform_rejections: dict[str, dict[str, Any]] = {}
        self._master_knowledge = ""
        self._installed_app_names: tuple[str, ...] | None = None
        self._executor = bundle.make_executor(platform)
        self._target_verify_pool = _WORKER_VERIFY_POOL
        try:
            self._visualizer = bundle.make_action_visualizer(platform)
        except Exception:  # noqa: BLE001 - action visualization is cosmetic
            self._visualizer = None
        if perception_mode == "vision-only":
            setattr(self._executor, "disable_dom_snap", True)

    def run(
        self,
        goal: str,
        *,
        knowledge: str = "",
        access_context: str = "",
        page_url: str = "",
        page_title: str = "",
    ) -> ToolAgentRun:
        self._worker_access_context = access_context.strip()
        self._master_knowledge = knowledge
        self._task_goal = goal
        self._task_page_url = page_url
        self._access_log_redactions = _access_log_redactions(access_context)
        executor = getattr(self, "_executor", None)
        if executor is not None:
            setattr(
                executor,
                "sensitive_text_values",
                self._access_log_redactions,
            )
        try:
            return self._run(
                goal,
                knowledge=knowledge,
                page_url=page_url,
                page_title=page_title,
            )
        finally:
            self._clear_action_visualizer()
            self._worker_access_context = ""
            self._master_knowledge = ""
            self._task_goal = ""
            self._task_page_url = ""
            self._access_log_redactions = ()
            if executor is not None:
                setattr(executor, "sensitive_text_values", ())
            journals = getattr(self, "_worker_journals", None)
            if journals is not None:
                journals.clear()
            last_frames = getattr(self, "_worker_last_frames", None)
            if last_frames is not None:
                last_frames.clear()
            platform_rejections = getattr(self, "_worker_platform_rejections", None)
            if platform_rejections is not None:
                platform_rejections.clear()

    def _run(self, goal: str, *, knowledge: str = "", page_url: str = "", page_title: str = "") -> ToolAgentRun:
        self._trace(
            "runtime_started",
            goal=goal,
            perception_mode=self.perception_mode,
            max_turns=int(getattr(self, "max_turns", 50)),
            multi_action=bool(getattr(self, "allow_multi_action", False)),
            master_model=self.master_cfg.model,
            worker_model=self.worker_cfg.model,
        )
        task_context = {
            "goal": goal,
            "page": {"url": page_url, "title": page_title},
            "platform": self._platform_prompt_context(),
            "application_knowledge": knowledge or "(none)",
        }
        final_ref = None
        final_summary = ""
        final_effect: Literal["mutation", "data", "ui_state", "none"] = "none"
        phase: Literal["completed", "failed"] = "failed"
        orchestration = WorkerOrchestrationContext(
            data_store=self.data_store,
            run_gui_worker=self._run_worker_with_local_replanning,
            trace=self._trace,
        )
        try:
            self._raise_if_cancelled()
            program = compile_master_program(
                llm=self.master,
                system_prompt=_MASTER_SYSTEM,
                task_context=task_context,
                max_attempts=self.max_compile_attempts,
                cache_system_prompt=getattr(
                    self,
                    "_master_explicit_cache",
                    False,
                ),
                on_event=lambda event, payload: self._trace(event, **payload),
            )
            self._trace(
                "master_program_generated",
                compile_attempts=program.attempts,
                source=program.source,
            )
            self._raise_if_cancelled()
            execution_no = 1
            self._trace(
                "master_program_execution_started",
                execution=execution_no,
                source=program.source,
            )
            execution = execute_master_program(program.source, orchestration)
            # Worker exceptions may be projected into an execution error by the
            # deterministic sandbox. Re-check the cooperative cancellation flag
            # here so an interrupted run is still recorded unambiguously.
            self._raise_if_cancelled()
            if execution.error:
                self._trace(
                    "master_program_error",
                    execution=execution_no,
                    error=execution.error,
                )
                final_summary = (
                    "Reviewed Master program failed during deterministic execution: "
                    f"{execution.error}"
                )
            else:
                assert execution.terminal is not None
                terminal = execution.terminal
                self._trace(
                    "master_program_completed",
                    execution=execution_no,
                    phase=terminal.phase,
                    summary=terminal.summary,
                    result_ref=terminal.result_ref,
                    effect=terminal.effect,
                )
                final_summary = terminal.summary
                if terminal.phase == "completed":
                    final_ref = self.data_store.result_descriptor(terminal.result_ref)
                    final_effect = terminal.effect
                    phase = "completed"
        except (KeyboardInterrupt, _RuntimeCancelled):
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
            effect=final_effect,
            result_ref=final_ref.ref if final_ref is not None else "",
            turns_used=int(getattr(self, "_frame_no", 0)),
            max_turns=int(getattr(self, "max_turns", 50)),
        )
        output = self.data_store.result_value(final_ref.ref) if final_ref is not None else None
        run = ToolAgentRun(
            phase=phase,
            summary=final_summary,
            effect=final_effect,
            output=output,
            result_ref=final_ref,
            trace=self.trace,
            master_model=self.master_cfg.model,
            worker_model=self.worker_cfg.model,
            perception_model=self.materializer.model,
            perception_mode=self.perception_mode,
        )
        self._write_artifacts(run)
        replay = write_replay_artifact(self.log_dir)
        with (self.log_dir / "tool_agent.log").open("a", encoding="utf-8") as stream:
            stream.write(
                f"\n[Replay] {replay.status.upper()} · {replay.summary}\n"
            )
        return run

    def _raise_if_cancelled(self) -> None:
        callback = getattr(self, "_stop_requested", None)
        if callback is not None and callback():
            raise _RuntimeCancelled

    @staticmethod
    def _is_verified_empty(outcome: WorkerOutcome) -> bool:
        collection = outcome.collection_ref
        return bool(
            outcome.phase == "completed"
            and collection is not None
            and collection.row_count == 0
            and collection.coverage.get("scope_status") == "met"
            and collection.coverage.get("status") == "complete"
        )

    @staticmethod
    def _worker_replan_reason(outcome: WorkerOutcome) -> str:
        if ToolAgentRuntime._is_verified_empty(outcome):
            return "The prior Worker established the requested scope but produced no result."
        if outcome.failure_kind == "platform_rejected":
            return ""
        if outcome.phase == "failed":
            return f"The prior Worker failed to satisfy the subgoal: {outcome.summary}"
        return ""

    @staticmethod
    def _replanned_worker_id(worker_id: str, replan_no: int) -> str:
        suffix = f"_replan_{replan_no}"
        if len(worker_id) + len(suffix) <= 64:
            return worker_id + suffix
        digest = hashlib.sha256(worker_id.encode()).hexdigest()[:8]
        prefix_size = 64 - len(suffix) - len(digest) - 1
        return f"{worker_id[:prefix_size]}_{digest}{suffix}"

    @staticmethod
    def _worker_revision_issues(
        original: WorkerSpec,
        revised: WorkerSpec,
        *,
        prior_outcome: WorkerOutcome | None = None,
        attempted_specs: list[WorkerSpec] | None = None,
    ) -> list[str]:
        issues: list[str] = []
        if revised == original:
            issues.append("replacement WorkerSpec is identical to the prior WorkerSpec")
        elif revised in (attempted_specs or []):
            issues.append("replacement WorkerSpec has already been attempted")
        if revised.profile != original.profile:
            issues.append("profile is immutable across runtime redelegation")
        if revised.goal != original.goal:
            issues.append("goal is immutable across runtime redelegation")
        if revised.success_criteria != original.success_criteria:
            issues.append("success_criteria are immutable across runtime redelegation")
        if revised.input_refs != original.input_refs:
            issues.append("input_refs are immutable across runtime redelegation")
        if len(revised.data_requirements) != len(original.data_requirements):
            issues.append("data requirement count is immutable across runtime redelegation")
            return issues
        immutable_fields = (
            "id",
            "description",
            "target_label",
            "scope",
            "row_schema",
            "field_sources",
            "field_types",
            "filters",
            "coverage",
        )
        for index, (before, after) in enumerate(
            zip(original.data_requirements, revised.data_requirements, strict=True)
        ):
            for field_name in immutable_fields:
                if getattr(before, field_name) != getattr(after, field_name):
                    issues.append(
                        f"data_requirements[{index}].{field_name} is immutable"
                    )
        if (
            prior_outcome is not None
            and ToolAgentRuntime._is_verified_empty(prior_outcome)
            and revised.acquisition_filters == original.acquisition_filters
        ):
            issues.append(
                "acquisition_filters must change after an authoritative empty result"
            )
        try:
            ToolAgentRuntime._validate_worker_spec(revised)
        except Exception as exc:  # noqa: BLE001 - one revision diagnostic channel
            issues.append(f"replacement WorkerSpec is not executable: {exc}")
        return issues

    def _worker_recovery_experience(
        self,
        worker_id: str,
        *,
        logical_worker_id: str = "",
    ) -> dict[str, Any]:
        """Project bounded semantic experience without screenshots or coordinates."""

        events: list[dict[str, Any]] = []
        for entry in self.trace:
            entry_worker_id = str(entry.get("worker_id") or "")
            in_logical_chain = bool(
                logical_worker_id
                and (
                    entry_worker_id == logical_worker_id
                    or entry_worker_id.startswith(f"{logical_worker_id}_replan_")
                )
            )
            if entry_worker_id != worker_id and not in_logical_chain:
                continue
            event = str(entry.get("event") or "")
            if event == "worker_decision":
                args = {
                    key: value
                    for key, value in dict(entry.get("args") or {}).items()
                    if key not in {"x", "y", "state"}
                }
                events.append({
                    "event": event,
                    "step": entry.get("step"),
                    "state": entry.get("state"),
                    "tool": entry.get("tool"),
                    "args": args,
                })
            elif event == "runtime_action":
                events.append({
                    "event": event,
                    "tool": entry.get("tool"),
                    "action_type": entry.get("action_type"),
                    "status": entry.get("status"),
                    "no_effect": entry.get("no_effect"),
                })
            elif event == "observe":
                events.append({
                    "event": event,
                    "frame_id": entry.get("frame_id"),
                    "url": entry.get("url"),
                    "requirement_scopes": entry.get("requirement_scopes"),
                    "collections": entry.get("collections"),
                    "missing_requirements": entry.get("missing_requirements"),
                })
        frame = getattr(self, "_worker_last_frames", {}).get(worker_id)
        current = {}
        if frame is not None:
            current = {
                "frame_id": frame.frame_id,
                "url": frame.url,
                "title": frame.title,
                "applied_filters": frame.applied_filters,
                "requirement_scopes": frame.requirement_scopes,
                "collections": [
                    item.model_dump(mode="json") for item in frame.collections
                ],
                "missing_requirements": frame.missing_requirements,
            }
        return {"events": events[-18:], "current_observation": current}

    def _revise_worker_spec(
        self,
        *,
        logical_worker_id: str,
        prior_worker_id: str,
        original_spec: WorkerSpec,
        prior_outcome: WorkerOutcome,
        replan_reason: str,
        replan_no: int,
        prior_revisions: list[WorkerSpec],
    ) -> WorkerSpec:
        generator = self.master.bind(
            response_format={"type": "json_object"},
            max_tokens=6_000,
            extra_body={"enable_thinking": False},
        )
        rejected: dict[str, Any] | None = None
        validation_issues: list[str] = []
        for attempt in range(1, 3):
            payload: dict[str, Any] = {
                "logical_worker_id": logical_worker_id,
                "prior_worker_id": prior_worker_id,
                "replan_no": replan_no,
                "replan_reason": replan_reason,
                "prior_worker_spec": original_spec.model_dump(mode="json"),
                "prior_outcome": prior_outcome.model_dump(mode="json"),
                "immutable_contract": {
                    "profile": original_spec.profile,
                    "goal": original_spec.goal,
                    "success_criteria": original_spec.success_criteria,
                    "input_refs": original_spec.input_refs,
                    "data_requirements": [
                        {
                            "id": item.id,
                            "description": item.description,
                            "target_label": item.target_label,
                            "scope": item.scope,
                            "row_schema": item.row_schema,
                            "field_sources": item.field_sources,
                            "field_types": item.field_types,
                            "filters": item.filters,
                            "coverage": item.coverage,
                        }
                        for item in original_spec.data_requirements
                    ],
                },
                "application_knowledge": getattr(self, "_master_knowledge", "") or "(none)",
                "platform": self._platform_prompt_context(),
                "execution_experience": self._worker_recovery_experience(
                    prior_worker_id,
                    logical_worker_id=logical_worker_id,
                ),
                "attempted_worker_specs": [
                    item.model_dump(mode="json") for item in prior_revisions
                ],
            }
            if rejected is not None:
                payload["rejected_worker_spec"] = rejected
                payload["validation_issues"] = validation_issues
            messages = [
                cacheable_system_message(
                    _MASTER_REDELEGATE_SYSTEM,
                    enabled=getattr(self, "_master_explicit_cache", False),
                ),
                HumanMessage(content=json.dumps(payload, ensure_ascii=False)),
            ]
            started_at = time.perf_counter()
            response = generator.invoke(messages)
            elapsed = time.perf_counter() - started_at
            candidate_payload: dict[str, Any] = {}
            try:
                decoded = parse_json_object(response.content)
                candidate_payload = decoded.get("worker_spec", decoded)
                candidate = WorkerSpec.model_validate(candidate_payload)
                validation_issues = self._worker_revision_issues(
                    original_spec,
                    candidate,
                    prior_outcome=prior_outcome,
                    attempted_specs=prior_revisions,
                )
            except Exception as exc:  # noqa: BLE001 - reviewed recovery protocol
                candidate = None
                validation_issues = [f"{type(exc).__name__}: {exc}"]
            self._trace(
                "master_worker_revision_attempt",
                logical_worker_id=logical_worker_id,
                prior_worker_id=prior_worker_id,
                replan_no=replan_no,
                attempt=attempt,
                candidate=candidate_payload,
                diagnostics=validation_issues,
                llm_elapsed_s=round(elapsed, 3),
                token_usage=response_usage(response),
                context_reports=diagnostic_prompt_reports(
                    "tool_agent.master_redelegate",
                    messages,
                    response,
                    parsed={
                        "worker_spec": candidate_payload,
                        "diagnostics": validation_issues,
                    },
                    schema="Replacement WorkerSpec",
                ),
            )
            if candidate is not None and not validation_issues:
                return candidate
            rejected = candidate_payload
        raise ValueError(
            "Master could not produce a valid replacement WorkerSpec: "
            + "; ".join(validation_issues)
        )

    def _run_worker_with_local_replanning(
        self,
        worker_id: str,
        spec: WorkerSpec,
    ) -> WorkerOutcome:
        if self._turn_budget_exhausted():
            return self._turn_budget_failure(worker_id=worker_id, steps=0)
        current_id = worker_id
        current_spec = spec
        prior_revisions: list[WorkerSpec] = [spec]
        max_replans = max(0, int(getattr(self, "max_subgoal_replans", 0)))
        breakers = getattr(self, "_logical_action_breakers", None)
        if breakers is None:
            self._logical_action_breakers = {}
            breakers = self._logical_action_breakers
        breakers[worker_id] = WorkerActionCircuitBreaker()
        logical_step_budget = min(
            _MAX_LOGICAL_WORKER_STEPS,
            spec.max_steps + _LOCAL_REPLAN_EXTRA_STEPS,
        )
        consumed_steps = 0
        for replan_no in range(max_replans + 1):
            remaining_steps = logical_step_budget - consumed_steps
            if remaining_steps <= 0:
                return WorkerOutcome(
                    phase="failed",
                    summary=(
                        "The logical Worker exhausted its shared execution budget "
                        f"after {consumed_steps} steps."
                    ),
                    steps=consumed_steps,
                )
            if current_spec.max_steps > remaining_steps:
                current_spec = current_spec.model_copy(
                    update={"max_steps": remaining_steps}
                )
            outcome = self._run_worker(current_id, current_spec)
            consumed_steps += outcome.steps
            if self._turn_budget_exhausted() and outcome.phase == "failed":
                if outcome.summary.startswith("Worker exceeded "):
                    return self._turn_budget_failure(
                        worker_id=current_id,
                        steps=outcome.steps,
                    )
                return outcome
            replan_reason = self._worker_replan_reason(outcome)
            if not replan_reason:
                return outcome
            self._trace(
                "master_worker_replan_requested",
                logical_worker_id=worker_id,
                worker_id=current_id,
                replan_no=replan_no,
                reason=replan_reason,
                spec=current_spec.model_dump(mode="json"),
                outcome=outcome.model_dump(mode="json"),
            )
            if replan_no >= max_replans:
                return WorkerOutcome(
                    phase="failed",
                    summary=(
                        "The Worker subgoal remained unsatisfied after the Master "
                        f"exhausted its local strategy budget. Last outcome: {outcome.summary}"
                    ),
                    steps=consumed_steps,
                )
            if consumed_steps >= logical_step_budget:
                self._trace(
                    "master_worker_budget_exhausted",
                    logical_worker_id=worker_id,
                    worker_id=current_id,
                    consumed_steps=consumed_steps,
                    step_budget=logical_step_budget,
                )
                return WorkerOutcome(
                    phase="failed",
                    summary=(
                        "The logical Worker exhausted its shared execution budget "
                        f"after {consumed_steps} steps."
                    ),
                    steps=consumed_steps,
                )
            try:
                revised = self._revise_worker_spec(
                    logical_worker_id=worker_id,
                    prior_worker_id=current_id,
                    original_spec=current_spec,
                    prior_outcome=outcome,
                    replan_reason=replan_reason,
                    replan_no=replan_no + 1,
                    prior_revisions=prior_revisions,
                )
            except Exception as exc:  # noqa: BLE001 - becomes typed Worker failure
                return WorkerOutcome(
                    phase="failed",
                    summary=f"Master worker redelegation failed: {type(exc).__name__}: {exc}",
                    steps=consumed_steps,
                )
            prior_revisions.append(revised)
            next_id = self._replanned_worker_id(worker_id, replan_no + 1)
            self._trace(
                "master_worker_redelegated",
                logical_worker_id=worker_id,
                prior_worker_id=current_id,
                worker_id=next_id,
                replan_no=replan_no + 1,
                prior_outcome=outcome.model_dump(mode="json"),
                spec=revised.model_dump(mode="json"),
            )
            current_id = next_id
            current_spec = revised
        raise AssertionError("unreachable worker replanning loop")

    def _show_action(self, action: Any) -> None:
        """Best-effort live cursor update through the platform visualizer."""
        visualizer = getattr(self, "_visualizer", None)
        if visualizer is None or action is None:
            return
        try:
            visualizer.show_action(action)
        except Exception:  # noqa: BLE001 - visualization must never block execution
            pass

    def _clear_action_visualizer(self) -> None:
        visualizer = getattr(self, "_visualizer", None)
        if visualizer is None:
            return
        try:
            visualizer.clear()
        except Exception:  # noqa: BLE001 - teardown remains best-effort
            pass

    def _run_worker(self, worker_id: str, spec: WorkerSpec) -> WorkerOutcome:
        self._validate_worker_spec(spec)
        self._active_worker_id = worker_id
        logical_worker_id = re.sub(r"_replan_\d+$", "", worker_id)
        journals = getattr(self, "_worker_journals", None)
        if journals is None:
            self._worker_journals = {}
            journals = self._worker_journals
        journal = journals.setdefault(
            logical_worker_id,
            WorkerJournal(worker_id=logical_worker_id),
        )
        attempts = getattr(self, "_worker_attempts", None)
        if attempts is None:
            self._worker_attempts = {}
            attempts = self._worker_attempts
        attempt = int(attempts.get(logical_worker_id) or 0) + 1
        attempts[logical_worker_id] = attempt
        retained_events = len(journal.events)
        if attempt > 1:
            journal.record_replan(attempt=attempt)
        self._trace(
            "worker_started",
            worker_id=worker_id,
            attempt=attempt,
            retained_memory_events=retained_events,
            profile=spec.profile,
            goal=spec.goal,
            success_criteria=spec.success_criteria,
            requirement_ids=[item.id for item in spec.data_requirements],
            action_names=[item.name for item in spec.actions],
            max_steps=spec.max_steps,
        )
        active_actions = self._initial_worker_actions(spec)
        breakers = getattr(self, "_logical_action_breakers", None)
        if breakers is None:
            self._logical_action_breakers = {}
            breakers = self._logical_action_breakers
        circuit_breaker = breakers.setdefault(
            logical_worker_id,
            WorkerActionCircuitBreaker(),
        )
        for step in range(1, spec.max_steps + 1):
            self._raise_if_cancelled()
            if self._turn_budget_exhausted():
                return self._turn_budget_failure(
                    worker_id=worker_id,
                    steps=step - 1,
                )
            frame, png = self._observe(spec)
            last_frames = getattr(self, "_worker_last_frames", None)
            if last_frames is None:
                self._worker_last_frames = {}
                last_frames = self._worker_last_frames
            last_frames[worker_id] = frame
            ready_collection = self._ready_collection(spec, frame)
            if ready_collection is not None and ready_collection.row_count == 0:
                self._trace(
                    "worker_empty_collection",
                    step=step,
                    profile=spec.profile,
                    collection_ref=ready_collection.model_dump(mode="json"),
                )
                return WorkerOutcome(
                    phase="completed",
                    summary=(
                        "The requested collection scope completed with an authoritative "
                        "empty result."
                    ),
                    collection_ref=ready_collection,
                    steps=step - 1,
                )
            worker_tools = self._worker_tools_for_frame(spec, active_actions, frame)
            patch_turn = 0
            guard_repair_turn = 0
            circuit_decision = None
            same_frame_feedback: dict[str, Any] | None = None
            while True:
                messages, context_reports = self._worker_messages(
                    spec=spec,
                    active_actions=active_actions,
                    journal=journal,
                    frame=frame,
                    png=png,
                    same_frame_feedback=same_frame_feedback,
                )
                response = None
                state = None
                call = None
                calls: list[dict[str, Any]] = []
                protocol_schema = (
                    "WorkerState + ordered actions"
                    if getattr(self, "allow_multi_action", False)
                    else "WorkerState + exactly one tool call"
                )
                llm_elapsed_s = 0.0
                token_usage: dict[str, int] = {}
                for attempt in range(2):
                    started_at = time.perf_counter()
                    response = self.worker.bind_tools(
                        worker_tools,
                        tool_choice="required",
                        parallel_tool_calls=False,
                        extra_body={"enable_thinking": False},
                    ).invoke(messages)
                    llm_elapsed_s = time.perf_counter() - started_at
                    token_usage = response_usage(response)
                    try:
                        call = exactly_one_tool_call(response)
                        raw_state = call["args"].pop("state", None)
                        if call["name"] == "continue_with_actions":
                            raw_actions = call["args"].get("actions") or []
                            if not isinstance(raw_actions, list):
                                raise ProtocolError("actions must be an ordered list")
                            calls = [{
                                "name": str(item.get("name") or ""),
                                "args": normalize_action_arguments(
                                    dict(item.get("args") or {})
                                ),
                            } for item in raw_actions if isinstance(item, dict)]
                            if len(calls) != len(raw_actions):
                                raise ProtocolError("every action item must be an object")
                        else:
                            call["args"] = normalize_action_arguments(call["args"])
                            calls = [call]
                        if call["name"] == "continue_with_actions":
                            self._validate_multi_action_calls(calls, active_actions)
                        state, state_source, state_compatibility = self._decode_worker_state(
                            raw_state=raw_state,
                            content=getattr(response, "content", ""),
                            call=call,
                            frame=frame,
                            spec=spec,
                        )
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
                                "tool_agent.worker.protocol_repair", messages, response,
                                schema=protocol_schema,
                            ) + context_reports,
                        )
                        if attempt:
                            raise
                        messages = [*messages, response, HumanMessage(content=(
                            "Protocol repair: the previous response was invalid. On this SAME frame, "
                            "emit exactly one required tool call including its state field. "
                            "No action was executed."
                        ))]
                assert response is not None and state is not None and call is not None
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
                    context_reports=[*context_reports, *diagnostic_prompt_reports(
                        "tool_agent.worker",
                        messages,
                        response,
                        parsed={
                            "state": state.model_dump(mode="json"),
                            "tool_calls": calls,
                        },
                        schema=protocol_schema,
                    )],
                    memory_event_count=len(journal.events),
                    context_chars=int(context_reports[0].get("after_chars") or 0),
                    state_source=state_source,
                    state_compatibility=state_compatibility,
                )
                circuit_decision = None
                if call["name"] == "request_action_patch":
                    patch_turn += 1
                    try:
                        if patch_turn > _MAX_ACTION_PATCHES_PER_FRAME:
                            raise ProtocolError("same-frame action patch limit exceeded")
                        parsed_patch = RequestActionPatchArgs.model_validate(call["args"])
                        added_action = materialize_action_patch(parsed_patch)
                        self._validate_action_spec(added_action)
                        self._validate_platform_action(added_action)
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
                            worker_tools = self._worker_tools_for_frame(
                                spec,
                                active_actions,
                                frame,
                            )
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
                    journal.record_patch(
                        step=step,
                        patch_turn=patch_turn,
                        payload=patch_payload,
                    )
                    same_frame_feedback = patch_payload
                    if patch_turn > _MAX_ACTION_PATCHES_PER_FRAME:
                        return WorkerOutcome(
                            phase="failed",
                            summary="Worker exceeded the same-frame action patch limit.",
                            steps=step - 1,
                        )
                    continue

                guarded_call = calls[0] if call["name"] == "continue_with_actions" else call
                action_spec = next(
                    (item for item in active_actions if item.name == guarded_call["name"]),
                    None,
                )
                if action_spec is not None:
                    resolved_guard_args = {
                        "description": action_spec.description,
                        **action_spec.fixed_args,
                        **guarded_call["args"],
                    }
                    circuit_decision = circuit_breaker.inspect(
                        tool=guarded_call["name"],
                        capability=action_spec.capability,
                        args=resolved_guard_args,
                        frame=frame,
                    )
                    if circuit_decision.blocked:
                        guard_repair_turn += 1
                        feedback = {
                            "status": "blocked_repeated_action",
                            "reason": circuit_decision.reason,
                            "prior_attempts": circuit_decision.prior_attempts,
                            "instruction": (
                                "Treat the Runtime guard as authoritative. Do not rephrase or "
                                "retry the blocked action; advance from the current observation "
                                "or choose a materially different capability."
                            ),
                        }
                        self._trace(
                            "worker_action_blocked",
                            step=step,
                            frame_id=frame.frame_id,
                            tool=guarded_call["name"],
                            args=guarded_call["args"],
                            signature=circuit_decision.signature,
                            progress=circuit_decision.progress,
                            prior_attempts=circuit_decision.prior_attempts,
                            reason=circuit_decision.reason,
                        )
                        journal.record_guard(
                            step=step,
                            repair_turn=guard_repair_turn,
                            tool=guarded_call["name"],
                            reason=circuit_decision.reason,
                        )
                        if guard_repair_turn > _MAX_ACTION_GUARD_REPAIRS_PER_FRAME:
                            return WorkerOutcome(
                                phase="failed",
                                summary=(
                                    "Worker repeated a circuit-blocked action after corrective "
                                    f"feedback: {guarded_call['name']}"
                                ),
                                steps=step - 1,
                            )
                        same_frame_feedback = feedback
                        continue
                if call["name"] == "continue_with_actions":
                    # The executor owns per-atomic-action attempt accounting.
                    circuit_decision = None
                break
            try:
                if call["name"] == "continue_with_actions":
                    result_payload, terminal = self._execute_multi_action_calls(
                        worker_id=worker_id,
                        spec=spec,
                        actions=active_actions,
                        calls=calls,
                        state=state,
                        step=step,
                        frame=frame,
                        png=png,
                        journal=journal,
                        circuit_breaker=circuit_breaker,
                    )
                else:
                    result_payload, terminal = self._execute_worker_tool(
                        spec,
                        active_actions,
                        call,
                        png,
                        frame,
                    )
            except Exception as exc:  # noqa: BLE001 - feed capability failure back into ReAct
                result_payload = {
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                    "recovery": (
                        "Use the error and current frame to choose a materially different GUI "
                        "action. Do not repeat the same failed call."
                    ),
                }
                terminal = None
                self._trace("worker_tool_error", step=step, tool=call["name"], error=result_payload["error"])
            if circuit_decision is not None:
                self._record_action_attempt(
                    circuit_breaker, circuit_decision, action_spec, result_payload
                )
            if call["name"] != "continue_with_actions":
                journal.record_turn(
                    step=step,
                    frame_id=frame.frame_id,
                    state=state,
                    tool=call["name"],
                    args=call["args"],
                    result=result_payload,
                )
            if terminal == "complete":
                descriptor = (
                    CollectionRef.model_validate(result_payload)
                    if spec.profile == "collector"
                    else None
                )
                self._trace(
                    "worker_complete",
                    step=step,
                    profile=spec.profile,
                    collection_ref=(
                        descriptor.model_dump(mode="json") if descriptor is not None else None
                    ),
                )
                return WorkerOutcome(
                    phase="completed",
                    summary=state.summary,
                    collection_ref=descriptor,
                    steps=step,
                )
            if terminal == "fail":
                reason = FailWorkerArgs.model_validate(call["args"]).reason
                return WorkerOutcome(phase="failed", summary=reason, steps=step)
            if terminal == "platform_rejected":
                reason = str(
                    result_payload.get("reason")
                    or "The platform rejected the requested action."
                )
                self._trace(
                    "worker_platform_rejected",
                    step=step,
                    profile=spec.profile,
                    reason=reason,
                    platform_feedback=result_payload.get("platform_feedback") or [],
                )
                return WorkerOutcome(
                    phase="failed",
                    summary=reason,
                    failure_kind="platform_rejected",
                    steps=step,
                )
        return WorkerOutcome(
            phase="failed",
            summary=f"Worker exceeded {spec.max_steps} steps",
            steps=spec.max_steps,
        )

    def _turn_budget_exhausted(self) -> bool:
        limit = getattr(self, "max_turns", None)
        return bool(
            isinstance(limit, int)
            and limit > 0
            and int(getattr(self, "_frame_no", 0)) >= limit
        )

    def _turn_budget_failure(self, *, worker_id: str, steps: int) -> WorkerOutcome:
        used = int(getattr(self, "_frame_no", 0))
        limit = int(getattr(self, "max_turns", used))
        self._trace(
            "runtime_turn_budget_exhausted",
            worker_id=worker_id,
            turns_used=used,
            max_turns=limit,
        )
        return WorkerOutcome(
            phase="failed",
            summary=f"Task exhausted its global turn budget ({used}/{limit}).",
            steps=steps,
        )

    @staticmethod
    def _decode_worker_state(
        *,
        raw_state: Any,
        content: Any,
        call: dict[str, Any],
        frame: MaterializedFrame,
        spec: WorkerSpec,
    ) -> tuple[WorkerState, str, list[str]]:
        """Decode new and legacy state carriers without another model call."""
        compatibility: list[str] = []
        if isinstance(raw_state, dict):
            try:
                return (
                    WorkerState.model_validate({
                        **raw_state,
                        "action_space_status": (
                            "missing_action"
                            if call["name"] == "request_action_patch"
                            else "sufficient"
                        ),
                        "missing_action": (
                            str(call["args"].get("reason") or "")
                            if call["name"] == "request_action_patch"
                            else ""
                        ),
                    }),
                    "tool_args",
                    compatibility,
                )
            except Exception as exc:  # noqa: BLE001 - try legacy carrier locally
                compatibility.append(f"invalid tool state: {exc}")
        elif raw_state is not None:
            compatibility.append("tool state was not an object")

        try:
            state = WorkerState.model_validate(parse_json_object(content))
            compatibility.append("used legacy assistant content state")
            return state, "content_compat", compatibility
        except Exception as exc:  # noqa: BLE001 - deterministic local fallback
            compatibility.append(f"assistant content state unavailable: {exc}")

        coverage: dict[str, str] = {}
        for requirement_id, scope in frame.requirement_scopes.items():
            coverage[requirement_id] = str(scope.get("status") or "unknown")
        for collection in frame.collections:
            coverage[collection.requirement_id] = str(
                collection.coverage.get("status") or coverage.get(collection.requirement_id) or "unknown"
            )
        action_by_name = {action.name: action for action in spec.actions}
        action = action_by_name.get(call["name"])
        instruction = str(
            call["args"].get("description")
            or (action.description if action is not None else "")
            or call["name"]
        )
        capability = action.capability if action is not None else ""
        if call["name"] == "complete":
            status = "completed"
        elif call["name"] == "fail":
            status = "failed"
        elif spec.profile == "collector" and any(
            value == "met" for value in coverage.values()
        ):
            status = "collecting"
        else:
            status = "exploring"
        return (
            WorkerState(
                status=status,
                summary=f"Runtime accepted {call['name']} from the current frame.",
                open_gaps=[] if status in {"completed", "failed"} else [instruction],
                coverage=coverage,
                action_space_status=(
                    "missing_action"
                    if call["name"] == "request_action_patch"
                    else "sufficient"
                ),
                missing_action=(
                    str(call["args"].get("reason") or "")
                    if call["name"] == "request_action_patch"
                    else ""
                ),
                next_instruction=instruction,
            ),
            "runtime_compat",
            compatibility,
        )

    def _observe(self, spec: WorkerSpec) -> tuple[MaterializedFrame, bytes]:
        self._frame_no += 1
        frame, png = self.materializer.observe(
            bundle=self.bundle,
            platform=self.platform,
            requirements=spec.data_requirements,
            acquisition_filters=spec.acquisition_filters,
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
            url=frame.url,
            title=frame.title,
            structured_surfaces=frame.structured_surfaces,
            control_count=len(frame.controls),
        )
        durable_frame = _redact_log_value(
            frame.model_dump(mode="json"),
            getattr(self, "_access_log_redactions", ()),
        )
        (self.log_dir / f"observation_tool_agent_{self._frame_no}.json").write_text(
            json.dumps(durable_frame, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return frame, png

    def _worker_system_prompt(
        self,
        spec: WorkerSpec,
        active_actions: list[DynamicActionSpec],
    ) -> str:
        # Keep reusable runtime instructions and deployment context at the front.
        # A redelegated Worker changes only the attempt contract at the suffix.
        prompt = _WORKER_SYSTEM
        application_knowledge = getattr(self, "_master_knowledge", "").strip()
        if application_knowledge:
            prompt += (
                "\n\n## Application knowledge (read-only execution context)\n"
                "Use relevant application facts to choose efficient navigation and "
                "interaction strategies. Treat the current screenshot and Observer "
                "state as authoritative for what is presently visible.\n"
                + application_knowledge
            )
        installed_apps = self._installed_applications()
        if installed_apps:
            prompt += (
                "\n\n## Installed applications\n"
                "Use only these exact Runtime-provided names with launch_app: "
                + json.dumps(installed_apps, ensure_ascii=False)
            )
        access_context = getattr(self, "_worker_access_context", "")
        if access_context:
            prompt += (
                "\n\n## Session access context (private runtime input)\n"
                "Use these deployment/access facts only when the current screenshot requires "
                "authentication. Never repeat credentials in state summaries, evidence, final "
                "results, or user-facing text.\n"
                + access_context
            )
        if getattr(self, "allow_multi_action", False):
            prompt += (
                "\n\n## Ordered multi-action mode\n"
                f"Continue through `continue_with_actions`. Use 2–{MAX_ORDERED_ACTIONS} actions when all targets "
                "are already visible and no later action depends on newly revealed UI; "
                "otherwise use one. Runtime executes serially and may discard a stale suffix. "
                "Chain only actions that preserve the current geometry, except that a focus "
                "action may precede operations on the same already-visible control. Put "
                "confirmation, traversal, and other surface-changing actions last. Include "
                "all safe required operations on a fully visible form. Use patch, completion, "
                "and failure tools separately."
            )
        spec_for_prompt = spec.model_dump(mode="json", exclude={"actions"})
        private_actions = {action.name: action for action in spec.actions if action.input_args}
        spec_for_prompt["action_contracts"] = [
            private_actions.get(action.name, action).model_dump(
                mode="json",
                include={"name", "fixed_args", "input_args"},
                exclude_defaults=True,
            )
            for action in active_actions
        ]
        prompt += (
            "\n\n## Worker attempt contract\n"
            "The bound tools are the authoritative action descriptions and argument "
            "schemas. This compact contract supplies only the subgoal, acceptance/data "
            "contract, and Runtime-bound action descriptors.\n"
            + json.dumps(spec_for_prompt, ensure_ascii=False)
        )
        return prompt

    def _worker_messages(
        self,
        *,
        spec: WorkerSpec,
        active_actions: list[DynamicActionSpec],
        journal: WorkerJournal,
        frame: MaterializedFrame,
        png: bytes,
        same_frame_feedback: dict[str, Any] | None,
    ) -> tuple[list[Any], list[dict[str, Any]]]:
        """Rebuild one frame from bounded Journal memory; never replay chat history."""
        memory = build_worker_memory_view(journal)
        projection = project_worker_context(
            memory=memory,
            frame=frame,
            same_frame_feedback=same_frame_feedback,
        )
        system_prompt = self._worker_system_prompt(spec, active_actions)
        stable_prompt, delimiter, attempt_prompt = system_prompt.partition(
            "## Worker attempt contract"
        )
        messages = [
            cacheable_system_message(
                stable_prompt,
                enabled=getattr(self, "_worker_explicit_cache", False),
                suffix=(delimiter + attempt_prompt if delimiter else ""),
            ),
            image_message(projection.text, png),
        ]
        return messages, [projection.report]

    @staticmethod
    def _ready_collection(
        spec: WorkerSpec,
        frame: MaterializedFrame,
    ) -> CollectionRef | None:
        """Return the current frame's single Runtime-verifiable collection."""

        if spec.profile != "collector" or not spec.data_requirements:
            return None
        requirement_id = spec.data_requirements[0].id
        ready = [
            collection
            for collection in frame.collections
            if collection.requirement_id == requirement_id
            and collection.coverage.get("scope_status") == "met"
            and collection.coverage.get("status") == "complete"
        ]
        return ready[0] if len(ready) == 1 else None

    def _worker_tools_for_frame(
        self,
        spec: WorkerSpec,
        actions: list[DynamicActionSpec],
        frame: MaterializedFrame,
    ) -> list[dict[str, Any]]:
        completion_mode: Literal["unavailable", "operator", "collector"]
        if spec.profile == "operator":
            completion_mode = "operator"
        elif self._ready_collection(spec, frame) is not None:
            completion_mode = "collector"
        else:
            completion_mode = "unavailable"
        return dynamic_worker_tools(
            actions,
            completion_mode=completion_mode,
            action_envelope=bool(getattr(self, "allow_multi_action", False)),
        )

    def _active_platform_rejection(self) -> dict[str, Any] | None:
        worker_id = str(getattr(self, "_active_worker_id", "") or "")
        rejections = getattr(self, "_worker_platform_rejections", None)
        if not worker_id or not isinstance(rejections, dict):
            return None
        item = rejections.get(worker_id)
        return item if isinstance(item, dict) else None

    @staticmethod
    def _record_action_attempt(
        breaker: WorkerActionCircuitBreaker,
        decision: Any,
        action: DynamicActionSpec | None,
        result: dict[str, Any],
    ) -> None:
        if result.get("candidate_commit"):
            breaker.reset()
            return
        effective_scroll = bool(
            action
            and action.capability == "scroll"
            and result.get("status") == "executed"
            and result.get("no_effect") is False
        )
        if not effective_scroll:
            breaker.record(decision)

    def _refreshable_action_suffix(
        self,
        capability: str,
        remaining: list[dict[str, Any]],
        action_by_name: dict[str, DynamicActionSpec],
    ) -> bool:
        refresh_controls = getattr(getattr(self, "_executor", None), "refresh_controls", None)
        capabilities = [capability, *(action_by_name[item["name"]].capability for item in remaining)]
        return bool(
            getattr(self, "perception_mode", "enhanced") == "enhanced"
            and callable(refresh_controls)
            and all(item in {"tap", "type", "clear_text", "press_enter"} for item in capabilities)
            and any(item != "tap" for item in capabilities)
            and all(
                action_by_name[call["name"]].capability not in {"tap", "type"}
                or call.get("_control_ref")
                for call in remaining
            )
        )

    def _refresh_next_action(
        self,
        call: dict[str, Any],
        action: DynamicActionSpec,
        frame: MaterializedFrame,
    ) -> bool:
        if action.capability not in {"tap", "type"}:
            return True
        try:
            controls = self._executor.refresh_controls()
        except Exception:  # noqa: BLE001 - stale coordinates must never continue
            return False
        if not isinstance(controls, list):
            return False
        ref = str(call.pop("_control_ref", "") or "")
        control = next((item for item in controls if item.get("ref") == ref), None)
        point = control.get("action_point") if control is not None else None
        if not isinstance(point, dict):
            point = control.get("rect") if control is not None else None
        if not isinstance(point, dict) or not all(
            isinstance(point.get(key), (int, float)) for key in ("x", "y")
        ):
            return False
        call["args"].update(x=float(point["x"]), y=float(point["y"]))
        frame.controls = controls
        return True

    @staticmethod
    def _validate_multi_action_calls(
        calls: list[dict[str, Any]],
        actions: list[DynamicActionSpec],
    ) -> None:
        if not 1 <= len(calls) <= MAX_ORDERED_ACTIONS:
            raise ProtocolError(f"action envelope must contain 1–{MAX_ORDERED_ACTIONS} actions")
        action_names = {action.name for action in actions}
        unknown = [call["name"] for call in calls if call["name"] not in action_names]
        if unknown:
            raise ProtocolError(
                "multi-action output may contain only executable action tools; got "
                + ", ".join(unknown)
            )

    def _page_identity(self) -> tuple[str, str]:
        client = getattr(self.platform, "client", None)
        page_info = getattr(client, "page_info", None)
        try:
            if callable(page_info):
                url, title = page_info()
                return str(url or ""), str(title or "")
        except Exception:  # noqa: BLE001 - optional short check
            pass
        return "", ""

    def _suffix_requires_reobservation(
        self,
        *,
        call: dict[str, Any],
        action: DynamicActionSpec,
        remaining: list[dict[str, Any]],
        action_by_name: dict[str, DynamicActionSpec],
        frame: MaterializedFrame | None = None,
    ) -> str:
        capability = action.capability
        refreshable = self._refreshable_action_suffix(
            capability, remaining, action_by_name,
        )
        if capability == "type" and not bool(getattr(
            getattr(self, "_executor", None), "type_suffix_safe", True,
        )) and not refreshable:
            return "type can reflow this platform; reobserve before continuing"
        if capability in {"type", "clear_text"}:
            return ""
        if capability == "tap":
            tap_args = {**action.fixed_args, **call["args"]}
            if frame is not None and self._stable_distinct_tap_targets(
                tap_args,
                remaining,
                action_by_name,
                frame,
            ):
                return ""
            if refreshable:
                return ""
            try:
                later_actions = [
                    (
                        action_by_name[item["name"]],
                        {
                            **action_by_name[item["name"]].fixed_args,
                            **item["args"],
                        },
                    )
                    for item in remaining
                    if action_by_name[item["name"]].capability != "clear_text"
                ]
                tap_type_suffix_safe = bool(getattr(
                    getattr(self, "_executor", None),
                    "tap_type_suffix_safe",
                    True,
                ))
                if tap_type_suffix_safe and later_actions and all(
                    later_action.capability == "type"
                    and hypot(
                        float(tap_args["x"]) - float(later_args["x"]),
                        float(tap_args["y"]) - float(later_args["y"]),
                    ) <= _MULTI_ACTION_TARGET_TOLERANCE
                    for later_action, later_args in later_actions
                ):
                    return ""
            except (KeyError, TypeError, ValueError):
                pass
        return (
            f"{capability} can invalidate coordinates for the remaining actions; "
            "reobserve before continuing"
        )

    @staticmethod
    def _stable_distinct_tap_targets(
        current_args: dict[str, Any],
        remaining: list[dict[str, Any]],
        action_by_name: dict[str, DynamicActionSpec],
        frame: MaterializedFrame,
    ) -> bool:
        """Prove that a tap batch targets distinct, non-commit controls."""

        if any(
            action_by_name[item["name"]].capability != "tap"
            for item in remaining
        ):
            return False
        controls = [
            control_at_point(args, frame)
            for args in [current_args, *(item["args"] for item in remaining)]
        ]
        if any(
            control is None
            or str(control.get("kind") or "").casefold()
            not in {"checkbox", "checkbox_input"}
            or control.get("form_action") == "commit"
            or control.get("query_action") not in (None, "")
            for control in controls
        ):
            return False
        return (
            len({id(control) for control in controls}) == len(controls)
            and all(
                control.get("selection_mode") == "multiple"
                for control in controls
            )
        )

    def _execute_multi_action_calls(
        self,
        *,
        worker_id: str,
        spec: WorkerSpec,
        actions: list[DynamicActionSpec],
        calls: list[dict[str, Any]],
        state: WorkerState,
        step: int,
        frame: MaterializedFrame,
        png: bytes,
        journal: WorkerJournal,
        circuit_breaker: WorkerActionCircuitBreaker,
    ) -> tuple[dict[str, Any], str | None]:
        """Execute one fused Worker decision as interruptible atomic actions."""

        action_by_name = {action.name: action for action in actions}
        if callable(getattr(getattr(self, "_executor", None), "refresh_controls", None)):
            for call in calls:
                action = action_by_name[call["name"]]
                if action.capability in {"tap", "type"}:
                    args = {**action.fixed_args, **call["args"]}
                    call["_control_ref"] = str(
                        (control_at_point(args, frame) or {}).get("ref") or ""
                    )
        current_png = png
        live_identity = self._page_identity()
        page_identity = (
            frame.url or live_identity[0],
            frame.title or live_identity[1],
        )
        executed = 0
        reason = ""
        terminal: str | None = None
        for index, action_call in enumerate(calls, start=1):
            action_spec = action_by_name[action_call["name"]]
            remaining = calls[index:]
            suffix_reason = (
                self._suffix_requires_reobservation(
                    call=action_call,
                    action=action_spec,
                    remaining=remaining,
                    action_by_name=action_by_name,
                    frame=frame,
                )
                if remaining
                else ""
            )
            refresh_suffix = bool(
                remaining and self._refreshable_action_suffix(
                    action_spec.capability, remaining, action_by_name,
                )
            )
            circuit_decision = circuit_breaker.inspect(
                tool=action_call["name"],
                capability=action_spec.capability,
                args={
                    "description": action_spec.description,
                    **action_spec.fixed_args,
                    **action_call["args"],
                },
                frame=frame,
            )
            if circuit_decision.blocked:
                reason = circuit_decision.reason
                journal.record_guard(
                    step=step,
                    repair_turn=index,
                    tool=action_call["name"],
                    reason=reason,
                )
                break
            self._trace(
                "runtime_action_started",
                worker_id=worker_id,
                step=step,
                batch_index=index,
                batch_total=len(calls),
                tool=action_call["name"],
                action_type=action_spec.capability,
                description=str(
                    action_call["args"].get("description")
                    or action_spec.description
                ),
            )
            try:
                result, terminal = self._execute_worker_tool(
                    spec,
                    actions,
                    action_call,
                    current_png,
                    frame,
                )
            except Exception as exc:  # noqa: BLE001 - abort suffix and reobserve
                result = {
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                }
                terminal = None
            self._record_action_attempt(
                circuit_breaker, circuit_decision, action_spec, result
            )
            executed += 1
            journal.record_turn(
                step=step,
                substep=index,
                frame_id=frame.frame_id,
                state=state,
                tool=action_call["name"],
                args=action_call["args"],
                result=result,
            )
            target_signal = result.get("target_signal")
            if (
                isinstance(target_signal, dict)
                and target_signal.get("status") == "off_target"
            ):
                actual = str(target_signal.get("actual_element") or "").strip()
                reason = (
                    "flash verifier reported off_target"
                    + (f" on {actual!r}" if actual else "")
                    + "; reobserve before continuing"
                )
                break
            if terminal is not None or result.get("status") in {"error", "failed"}:
                reason = str(
                    result.get("error")
                    or result.get("reason")
                    or f"atomic action ended with {terminal or 'failure'}"
                )
                break
            if not remaining:
                continue
            if suffix_reason:
                reason = suffix_reason
                break
            if refresh_suffix and not self._refresh_next_action(
                remaining[0], action_by_name[remaining[0]["name"]], frame,
            ):
                reason = "current controls could not rebind the action suffix"
                break
            try:
                current_png = self.platform.screenshot()
            except Exception as exc:  # noqa: BLE001 - never execute a blind suffix
                reason = f"latest screenshot failed: {type(exc).__name__}: {exc}"
                break
            next_page_identity = self._page_identity()
            if any(
                before and after and before != after
                for before, after in zip(page_identity, next_page_identity, strict=True)
            ):
                reason = "page identity changed before the action suffix completed"
                break
            page_identity = next_page_identity

        payload = {
            "status": "executed" if executed == len(calls) and not reason else "aborted",
            "planned_actions": len(calls),
            "executed_actions": executed,
        }
        event = (
            "worker_multi_action_completed"
            if payload["status"] == "executed"
            else "worker_multi_action_aborted"
        )
        if reason:
            payload["reason"] = reason
        self._trace(event, worker_id=worker_id, step=step, **payload)
        return payload, terminal

    def _execute_worker_tool(
        self,
        spec: WorkerSpec,
        actions: list[DynamicActionSpec],
        call: dict[str, Any],
        png: bytes,
        frame: MaterializedFrame | None = None,
    ) -> tuple[dict[str, Any], str | None]:
        if call["name"] == "complete":
            CompleteReadyWorkerArgs.model_validate(call["args"])
            rejection = self._active_platform_rejection()
            if rejection is not None:
                reason = str(rejection.get("message") or "The platform rejected the action.")
                return {
                    "status": "failed",
                    "reason": reason,
                    "platform_feedback": [rejection],
                }, "platform_rejected"
            if spec.profile == "collector":
                if frame is None:
                    raise ValueError("collector complete requires a current frame")
                frame_collection = self._ready_collection(spec, frame)
                if frame_collection is None:
                    raise ValueError(
                        "collector complete is unavailable until the current frame "
                        "contains one scope-met, complete CollectionRef"
                    )
                descriptor = self.data_store.collection_descriptor(frame_collection.ref)
                requirement = spec.data_requirements[0]
                if descriptor.requirement_id != requirement.id:
                    raise ValueError(
                        f"CollectionRef {descriptor.ref!r} belongs to requirement "
                        f"{descriptor.requirement_id!r}, not {requirement.id!r}"
                    )
                if descriptor.coverage.get("scope_status") != "met":
                    raise ValueError(
                        f"CollectionRef {descriptor.ref!r} scope is not established"
                    )
                if descriptor.coverage.get("status") != "complete":
                    raise ValueError(
                        f"CollectionRef {descriptor.ref!r} coverage is not complete"
                    )
                return descriptor.model_dump(mode="json"), "complete"
            return {"status": "completed"}, "complete"
        if call["name"] == "fail":
            parsed = FailWorkerArgs.model_validate(call["args"])
            rejection = self._active_platform_rejection()
            if rejection is not None:
                reason = str(rejection.get("message") or parsed.reason)
                return {
                    "status": "failed",
                    "reason": reason,
                    "platform_feedback": [rejection],
                }, "platform_rejected"
            return {"status": "failed", "reason": parsed.reason}, "fail"
        action_by_name = {item.name: item for item in actions}
        action_spec = action_by_name.get(call["name"])
        if action_spec is None:
            raise ProtocolError(f"unknown Worker tool {call['name']!r}")
        parameters = dynamic_action_tool(action_spec)["function"]["parameters"]
        call_args = dict(call["args"])
        if "description" in parameters.get("properties", {}) and not str(
            call_args.get("description") or ""
        ).strip():
            # The action contract already supplies an unambiguous description.
            # Recover this single lossless omission instead of spending another
            # visual-policy turn on provider schema noncompliance.
            call_args["description"] = action_spec.description
        validate(instance=call_args, schema=parameters)
        full_args = {**action_spec.fixed_args, **call_args}
        if call["name"] == "runtime_open_url":
            self._validate_runtime_open_url(
                str(full_args.get("url") or ""),
                spec=spec,
                frame=frame,
            )
        if action_spec.capability == "launch_app":
            self._validate_runtime_launch_app(str(full_args.get("app") or ""))
        if action_spec.capability in _EXECUTABLE_CAPABILITIES:
            spatial = action_spec.capability in _SPATIAL_CAPABILITIES
            for coordinate in ("x", "y"):
                value = full_args.get(coordinate)
                if spatial and value is not None and not 0 <= float(value) < 1000:
                    raise ValueError(f"{action_spec.name}: {coordinate} must be in [0, 1000)")
            action_payload = {
                "action_type": _ACTION_TYPES.get(
                    action_spec.capability,
                    action_spec.capability,
                ),
                "description": full_args.pop("description", action_spec.description),
                **full_args,
            }
            bundle = getattr(self, "bundle", None)
            make_action = getattr(bundle, "make_action", None)
            action = (
                make_action(action_payload)
                if callable(make_action)
                else BaseAction.model_validate(action_payload)
            )
            decision = BaseActionDecision(action=action)
            # Enhanced adapters may invisibly correct a visual near-miss using
            # current rendered-control geometry. Vision-only frames contain no
            # controls and their executor has DOM snap disabled, so their model
            # coordinates remain untouched end to end.
            ground = getattr(self._executor, "ground_coordinates", None)
            if (
                getattr(self, "perception_mode", "enhanced") == "enhanced"
                and spatial
                and frame is not None
                and frame.controls
                and callable(ground)
            ):
                try:
                    decision = ground(decision, frame.controls)
                except Exception:  # noqa: BLE001 - optional enhancement fails open
                    pass
            executed_action = decision.action
            # Grounding has already produced the best currently known point.
            # Show it before dispatch, then mirror the original runtime contract
            # by updating once more if the executor records a DOM snap.
            self._show_action(decision.action)
            executed = self._executor.execute(decision, png_bytes=png)
            if executed and has_snapped_point(decision):
                self._show_action(decision.action)
            verify_future = None
            verify_pool = getattr(self, "_target_verify_pool", None)
            if (
                executed
                and verify_pool is not None
                and executed_action.action_type in _TARGET_VERIFIED_ACTION_TYPES
                and executed_action.x is not None
                and executed_action.y is not None
            ):
                verify_future = verify_pool.submit(
                    verify_target,
                    png,
                    float(executed_action.x),
                    float(executed_action.y),
                    str(executed_action.description or ""),
                )
            elapsed, no_effect = settle_after_action(
                self.platform,
                png,
                action_type=executed_action.action_type,
            )
            target_signal: dict[str, Any] | None = None
            if verify_future is not None:
                try:
                    verification = TargetVerify.model_validate(
                        verify_future.result(timeout=VERIFY_TIMEOUT_S)
                    )
                    target_signal = {
                        "status": (
                            "on_target" if verification.on_target else "off_target"
                        ),
                        "actual_element": verification.actual_element,
                        "reason": verification.reason,
                    }
                except Exception as exc:  # noqa: BLE001 - optional verifier fails open
                    self._trace(
                        "worker_target_verify_error",
                        tool=call["name"],
                        error=f"{type(exc).__name__}: {exc}",
                    )
            platform_feedback: list[dict[str, Any]] = []
            feedback_reader = getattr(
                getattr(self.platform, "client", None),
                "consume_action_feedback",
                None,
            )
            if callable(feedback_reader):
                for item in feedback_reader():
                    if not isinstance(item, dict):
                        continue
                    status_code = item.get("status")
                    body = str(item.get("body") or "").strip()
                    decoded: Any = None
                    try:
                        decoded = json.loads(body) if body else None
                    except json.JSONDecodeError:
                        decoded = None
                    if isinstance(decoded, dict):
                        message = decoded.get("message") or decoded.get("error_message")
                        rejected = decoded.get("error") is True or decoded.get("success") is False
                        if rejected or message:
                            platform_feedback.append({
                                "status": int(status_code or 0),
                                "url": str(item.get("url") or ""),
                                "rejected": rejected,
                                "message": str(message or "").strip(),
                            })
                    elif isinstance(status_code, (int, float)) and int(status_code) >= 400:
                        platform_feedback.append({
                            "status": int(status_code),
                            "url": str(item.get("url") or ""),
                            "rejected": True,
                            "message": body[:500],
                        })
            rejected_feedback = any(
                item.get("rejected") is True for item in platform_feedback
            )
            if rejected_feedback:
                worker_id = str(getattr(self, "_active_worker_id", "") or "")
                if worker_id:
                    rejections = getattr(self, "_worker_platform_rejections", None)
                    if not isinstance(rejections, dict):
                        self._worker_platform_rejections = {}
                        rejections = self._worker_platform_rejections
                    rejection = dict(next(
                        item for item in reversed(platform_feedback)
                        if item.get("rejected") is True
                    ))
                    prior = rejections.get(worker_id)
                    same_rejection = bool(
                        isinstance(prior, dict)
                        and prior.get("message") == rejection.get("message")
                        and prior.get("url") == rejection.get("url")
                    )
                    rejection["occurrences"] = (
                        int(prior.get("occurrences") or 1) + 1
                        if same_rejection
                        else 1
                    )
                    rejections[worker_id] = rejection
            elif executed:
                worker_id = str(getattr(self, "_active_worker_id", "") or "")
                rejections = getattr(self, "_worker_platform_rejections", None)
                if worker_id and isinstance(rejections, dict):
                    rejections.pop(worker_id, None)
            payload = {
                "status": "executed" if executed and not rejected_feedback else "failed",
                "action_type": executed_action.action_type,
                "settle_seconds": round(elapsed, 3),
                "no_effect": no_effect,
                "grounding": getattr(decision.action, "snap", None),
            }
            if target_signal is not None:
                payload["target_signal"] = target_signal
            if platform_feedback:
                payload["platform_feedback"] = platform_feedback
            terminal = None
            rejection = self._active_platform_rejection()
            if (
                rejected_feedback
                and rejection is not None
                and int(rejection.get("occurrences") or 1) >= 2
            ):
                payload["reason"] = str(
                    rejection.get("message") or "The platform rejected the action."
                )
                terminal = "platform_rejected"
            if frame is not None and payload["status"] == "executed" and (
                payload["no_effect"] is False
                and is_candidate_commit(
                    executed_action.model_dump(mode="python", exclude_none=True),
                    frame,
                )
            ):
                payload["candidate_commit"] = True
            self._trace(
                "runtime_action",
                tool=call["name"],
                profile=spec.profile,
                **payload,
            )
            return payload, terminal
        raise ProtocolError(f"unsupported capability {action_spec.capability!r}")

    def _validate_runtime_open_url(
        self,
        candidate: str,
        *,
        spec: WorkerSpec,
        frame: MaterializedFrame | None,
    ) -> None:
        """Require baseline direct navigation to copy an externally supplied route."""

        candidate = candidate.strip()
        if not candidate:
            raise ValueError("runtime_open_url requires a non-empty URL")
        sources = [
            str(getattr(self, "_task_goal", "") or ""),
            str(getattr(self, "_task_page_url", "") or ""),
            str(getattr(self, "_master_knowledge", "") or ""),
            spec.goal,
            *spec.success_criteria,
            str(frame.url if frame is not None else ""),
        ]
        if any(candidate in source for source in sources if source):
            return

        candidate_parts = urlsplit(candidate)
        candidate_route = unquote(candidate_parts.path or "")
        if candidate_parts.query:
            candidate_route += f"?{candidate_parts.query}"
        supplied_routes: set[str] = set()
        for source in sources:
            for token in re.findall(r"https?://[^\s`'\"<>]+|/[A-Za-z0-9_./?=&%+-]+", source):
                clean = token.rstrip(".,;:)]}")
                parts = urlsplit(clean)
                route = unquote(parts.path or "")
                if parts.query:
                    route += f"?{parts.query}"
                if route:
                    supplied_routes.add(route)
        if candidate_route and candidate_route in supplied_routes:
            return
        raise ValueError(
            "runtime_open_url rejected an inferred URL/route. Use only an exact URL "
            "present in the task or application knowledge; otherwise navigate visually."
        )

    @staticmethod
    def _validate_worker_spec(spec: WorkerSpec) -> None:
        consumed_inputs: set[str] = set()
        for requirement in spec.data_requirements:
            Draft202012Validator.check_schema(requirement.row_schema)
        for action in spec.actions:
            ToolAgentRuntime._validate_action_spec(action)
            consumed_inputs.update(binding.input for binding in action.input_args.values())
            unknown_inputs = {
                binding.input for binding in action.input_args.values()
            }.difference(spec.input_refs)
            if unknown_inputs:
                raise ValueError(
                    f"{action.name}: input_args reference unknown input_refs "
                    f"{sorted(unknown_inputs)}"
                )
        unused_inputs = set(spec.input_refs).difference(consumed_inputs)
        if unused_inputs:
            raise ValueError(
                "input_refs must be consumed by deterministic action input_args: "
                f"{sorted(unused_inputs)}"
            )

    @staticmethod
    def _validate_action_spec(action: DynamicActionSpec) -> None:
        validate_dynamic_action_spec(action)

    def _platform_name(self) -> str:
        bundle = getattr(self, "bundle", None)
        return str(getattr(bundle, "platform", "browser") or "browser")

    def _platform_prompt_context(self) -> dict[str, Any]:
        capabilities = sorted(self._supported_capabilities())
        return {
            "name": self._platform_name(),
            "action_contracts": {
                capability: capability_parameters(capability)
                for capability in capabilities
            },
            "applications": list(self._installed_applications()),
        }

    def _installed_applications(self) -> tuple[str, ...]:
        cached = getattr(self, "_installed_app_names", None)
        if cached is not None:
            return cached
        if "launch_app" not in self._supported_capabilities():
            names: tuple[str, ...] = ()
        else:
            platform = getattr(self, "platform", None)
            lister = getattr(platform, "list_apps", None)
            if not callable(lister):
                lister = getattr(getattr(platform, "client", None), "list_apps", None)
            try:
                names = tuple(sorted({str(item).strip() for item in lister() if str(item).strip()}))
            except Exception:  # noqa: BLE001 - unavailable discovery disables launch safely
                names = ()
        self._installed_app_names = names
        return names

    def _validate_runtime_launch_app(self, candidate: str) -> None:
        candidate = candidate.strip()
        installed = self._installed_applications()
        if candidate and candidate in installed:
            return
        raise ValueError(
            "launch_app requires an exact Runtime-provided application name; "
            f"available: {list(installed)}"
        )

    def _supported_capabilities(self) -> frozenset[str]:
        capabilities = getattr(self, "_platform_capabilities", None)
        if capabilities is None:
            # Compatibility for focused replay/unit harnesses that construct the
            # runtime with object.__new__. Production instances always receive the
            # adapter-owned set in __init__.
            return frozenset(_EXECUTABLE_CAPABILITIES)
        return frozenset(capabilities)

    def _validate_platform_action(self, action: DynamicActionSpec) -> None:
        if action.capability not in self._supported_capabilities():
            raise ProtocolError(
                f"capability {action.capability!r} is unavailable on the "
                f"{self._platform_name()} adapter"
            )

    def _materialize_action_inputs(
        self,
        spec: WorkerSpec,
        action: DynamicActionSpec,
    ) -> DynamicActionSpec:
        if not action.input_args:
            return action
        resolved = dict(action.fixed_args)
        for argument, binding in action.input_args.items():
            try:
                value = self.data_store.result_value(spec.input_refs[binding.input])
                for part in binding.path:
                    value = value[part]
            except (KeyError, IndexError, TypeError) as exc:
                raise ValueError(
                    f"{action.name}: cannot resolve input binding for {argument!r} "
                    f"from {binding.input!r} path {binding.path!r}"
                ) from exc
            resolved[argument] = value
        materialized = action.model_copy(update={
            "fixed_args": resolved,
            "input_args": {},
        })
        self._validate_action_spec(materialized)
        return materialized

    def _initial_worker_actions(self, spec: WorkerSpec) -> list[DynamicActionSpec]:
        floor = worker_action_floor(self._supported_capabilities())
        reserved = _RUNTIME_WORKER_TOOL_NAMES.union(item.name for item in floor)
        collisions = reserved.intersection(item.name for item in spec.actions)
        if collisions:
            raise ValueError(f"WorkerSpec uses reserved runtime action names: {sorted(collisions)}")
        task_actions = [
            self._materialize_action_inputs(spec, action) for action in spec.actions
        ]
        for action in task_actions:
            self._validate_platform_action(action)
        return [*task_actions, *floor]

    @staticmethod
    def _event_layer(event: str) -> str:
        if event.startswith("master_"):
            return "master"
        if event in {"observe", "perception_extract"}:
            return "observer"
        if event.startswith("runtime_action"):
            return "action"
        if event.startswith("transform_"):
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
            return "Reviewed Master program is ready"
        if event == "master_program_execution_started":
            return f"Execute frozen Master program · attempt {payload.get('execution', '?')}"
        if event == "master_worker_dispatch":
            spec = payload.get("spec") if isinstance(payload.get("spec"), dict) else {}
            profile = str(spec.get("profile") or "operator")
            return (
                f"Dispatch {profile} GUI Worker {payload.get('worker_id', '?')}: "
                f"{payload.get('goal', '')}"
            ).strip()
        if event == "master_worker_replan_requested":
            return (
                f"Worker {payload.get('worker_id', '?')} did not satisfy its subgoal; "
                "request a different local strategy"
            )
        if event == "master_worker_revision_attempt":
            issues = list(payload.get("diagnostics") or [])
            return (
                f"Review replacement WorkerSpec attempt {payload.get('attempt', '?')}: "
                + (f"{len(issues)} issue(s)" if issues else "passed")
            )
        if event == "master_worker_redelegated":
            return (
                f"Redelegate {payload.get('logical_worker_id', '?')} to new GUI Worker "
                f"{payload.get('worker_id', '?')}"
            )
        if event == "worker_started":
            return (
                f"Start {payload.get('profile', 'operator')} Worker "
                f"{payload.get('worker_id', '?')} attempt {payload.get('attempt', 1)}: "
                f"{payload.get('goal', '')}"
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
        if event == "runtime_action_started":
            return (
                f"{payload.get('batch_index', '?')}/"
                f"{payload.get('batch_total', '?')} · "
                f"{payload.get('action_type', 'GUI')} · "
                f"{payload.get('description', '')}"
            )
        if event == "runtime_action":
            effect = "no effect" if payload.get("no_effect") else str(payload.get("status") or "")
            return f"{payload.get('action_type', 'GUI')} action via {payload.get('tool', '?')}: {effect}"
        if event == "transform_started":
            return (
                f"Start transform {payload.get('transform_id', '?')} from "
                f"{payload.get('inputs', [])}"
            )
        if event == "transform_completed":
            result = payload.get("result_ref") if isinstance(payload.get("result_ref"), dict) else {}
            return f"Transform {payload.get('transform_id', '?')} → {result.get('ref', '?')}"
        if event == "transform_reused":
            result = payload.get("result_ref") if isinstance(payload.get("result_ref"), dict) else {}
            return f"Reuse transform {payload.get('transform_id', '?')} → {result.get('ref', '?')}"
        if event == "transform_failed":
            return f"Transform {payload.get('transform_id', '?')} failed: {payload.get('error', '')}"
        if event == "worker_complete":
            collection = (
                payload.get("collection_ref")
                if isinstance(payload.get("collection_ref"), dict)
                else {}
            )
            suffix = f" with {collection.get('ref')}" if collection.get("ref") else ""
            return f"Worker completed{suffix}"
        if event == "worker_empty_collection":
            return "Worker completed the requested collection scope with zero rows"
        if event == "master_worker_result":
            outcome = payload.get("outcome") if isinstance(payload.get("outcome"), dict) else {}
            return f"Worker {payload.get('worker_id', '?')} returned {outcome.get('phase', '?')}"
        if event == "master_worker_retry":
            return f"Retry failed GUI Worker {payload.get('worker_id', '?')} with retained experience"
        if event == "subgoal_replan":
            return f"Retry failed GUI subgoal using retained experience: {payload.get('reason', '')}"
        if event == "master_program_completed":
            return f"Master program terminal: {payload.get('phase', '?')}"
        if event == "runtime_turn_budget_exhausted":
            return (
                "Task turn budget exhausted: "
                f"{payload.get('turns_used', '?')}/{payload.get('max_turns', '?')}"
            )
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
                f"perception={entry.get('perception_mode', '?')} · "
                f"max_turns={entry.get('max_turns', '?')}\n"
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
                    _token_metric(
                        int(usage.get("input") or 0),
                        int(usage.get("output") or 0),
                        int(usage.get("cached_input") or 0),
                    )
                )
            metric_text = f" ({' · '.join(metrics)})" if metrics else ""
            return (
                f"  [Review] attempt {entry.get('attempt', '?')} {verdict}{metric_text}"
            )
        if event == "master_program_generated":
            return (
                "Coding Master: frozen reviewed Python ready "
                f"({entry.get('compile_attempts', '?')} review attempts)"
            )
        if event == "master_program_execution_started":
            return f"MASTER  execute frozen program · attempt {entry.get('execution', '?')}"
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
        if event == "master_worker_replan_requested":
            return (
                f"Worker result: subgoal unsatisfied · {entry.get('worker_id', '?')}\n"
                f"Reason       : {entry.get('reason', '')}\n"
                "Master       : revise only this Worker's execution strategy"
            )
        if event == "master_worker_revision_attempt":
            issues = list(entry.get("diagnostics") or [])
            usage = entry.get("token_usage") if isinstance(entry.get("token_usage"), dict) else {}
            verdict = f"failed · {issues[0]}" if issues else "passed"
            metrics = []
            if entry.get("llm_elapsed_s"):
                metrics.append(f"{float(entry['llm_elapsed_s']):.1f}s")
            if usage:
                metrics.append(
                    _token_metric(
                        int(usage.get("input") or 0),
                        int(usage.get("output") or 0),
                        int(usage.get("cached_input") or 0),
                    )
                )
            suffix = f" ({' · '.join(metrics)})" if metrics else ""
            return (
                f"  [Redelegate] attempt {entry.get('attempt', '?')} "
                f"{verdict}{suffix}"
            )
        if event == "master_worker_redelegated":
            spec = entry.get("spec") if isinstance(entry.get("spec"), dict) else {}
            return (
                f"\n--- GUI Worker {entry.get('worker_id', '?')} "
                f"[{spec.get('profile', 'operator')}] · Master redelegation ---\n"
                f"Goal    : {spec.get('goal', '')}"
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
            page = str(entry.get("title") or entry.get("url") or "")
            surfaces = "; ".join(
                f"{item.get('kind', 'surface')} {item.get('caption') or '(untitled)'} "
                f"({item.get('row_count', '?')} rows)"
                for item in entry.get("structured_surfaces") or []
                if isinstance(item, dict)
            )
            return (
                f"\n--- Turn {turn_no} ---\n"
                + (f"Screenshot : {screenshot}\n" if screenshot else "")
                + f"Observation: {entry.get('mode', '?')} perception · "
                f"{entry.get('control_count', 0)} controls\n"
                + (f"Page       : {page}\n" if page else "")
                + (f"Surfaces   : {surfaces}\n" if surfaces else "")
                + f"Scope      : {scope_text}\n"
                + f"Collection : {collection_text}"
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
            cached_input = 0
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
                    cached_input += int(usage.get("cached_input") or 0)
            timing_text = " | ".join(f"{key}={value:.1f}s" for key, value in timings.items())
            metrics = ""
            if timing_text:
                metrics += f"\nTiming     : {timing_text}"
            if input_tokens or output_tokens:
                metrics += (
                    "\nTokens     : "
                    + _token_metric(input_tokens, output_tokens, cached_input)
                )
            context_chars = int(entry.get("context_chars") or 0)
            memory_events = int(entry.get("memory_event_count") or 0)
            context_text = (
                f"\nContext    : rebuilt for frame · {context_chars} chars · "
                f"{memory_events} journal events"
            )
            state_source = str(entry.get("state_source") or "")
            protocol_text = (
                f"\nProtocol   : state={state_source}"
                if state_source
                else ""
            )
            return (
                f"State      : {state.get('status', '?')} · {state.get('summary', '')}\n"
                f"Action plan: {entry.get('tool', '?')} · "
                f"{state.get('next_instruction', '')}"
                f"{context_text}"
                f"{protocol_text}"
                f"{metrics}"
            )
        if event == "runtime_action_started":
            return (
                f"Action start: {entry.get('batch_index', '?')}/"
                f"{entry.get('batch_total', '?')} · "
                f"{entry.get('action_type', 'GUI')} · "
                f"{entry.get('description', '')}"
            )
        if event in {"worker_multi_action_completed", "worker_multi_action_aborted"}:
            status = "completed" if event.endswith("completed") else "interrupted"
            reason = f" · {entry.get('reason')}" if entry.get("reason") else ""
            return (
                f"Action batch: {status} · {entry.get('executed_actions', 0)}/"
                f"{entry.get('planned_actions', 0)} executed{reason}"
            )
        if event == "runtime_action":
            effect = (
                "executed · effect unconfirmed"
                if entry.get("no_effect") and entry.get("status") == "executed"
                else entry.get("status", "")
            )
            settle = float(entry.get("settle_seconds") or 0)
            target_signal = entry.get("target_signal")
            target_text = ""
            if (
                isinstance(target_signal, dict)
                and target_signal.get("status") == "off_target"
            ):
                target_text = (
                    " · off_target"
                    + (
                        f" on {target_signal.get('actual_element')!r}"
                        if target_signal.get("actual_element")
                        else ""
                    )
                )
            return (
                f"Action     : {entry.get('action_type', '?')} via {entry.get('tool', '?')}\n"
                f"Result     : {effect}"
                + target_text
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
        if event == "worker_action_blocked":
            return (
                f"Action fuse: blocked {entry.get('tool', '?')} after "
                f"{entry.get('prior_attempts', '?')} equivalent attempts"
            )
        if event == "worker_complete":
            collection = (
                entry.get("collection_ref")
                if isinstance(entry.get("collection_ref"), dict)
                else {}
            )
            suffix = f" · {collection.get('ref')}" if collection.get("ref") else ""
            return f"Verification: completed{suffix}"
        if event == "worker_empty_collection":
            collection = (
                entry.get("collection_ref")
                if isinstance(entry.get("collection_ref"), dict)
                else {}
            )
            return (
                "Verification: exact scope complete · 0 rows · "
                f"{collection.get('ref', '?')}"
            )
        if event == "master_worker_result":
            outcome = entry.get("outcome") if isinstance(entry.get("outcome"), dict) else {}
            return f"Worker outcome: {entry.get('worker_id', '?')} · {outcome.get('phase', '?')}"
        if event == "master_worker_retry":
            return (
                f"Worker retry: {entry.get('worker_id', '?')} · retain prior journal, "
                "restart only this GUI subgoal"
            )
        if event == "subgoal_replan":
            return (
                "SUBGOAL replan · replay the same reviewed program and retry only "
                f"failed GUI work · {entry.get('reason', '')}"
            )
        if event == "transform_started":
            return (
                f"\n--- Transform {entry.get('transform_id', '?')} ---\n"
                f"Inputs  : {entry.get('inputs', [])}"
            )
        if event == "transform_completed":
            result = entry.get("result_ref") if isinstance(entry.get("result_ref"), dict) else {}
            return f"Result  : completed · {result.get('ref', '?')}"
        if event == "transform_reused":
            result = entry.get("result_ref") if isinstance(entry.get("result_ref"), dict) else {}
            return f"Result  : reused · {result.get('ref', '?')}"
        if event == "transform_failed":
            return f"Result  : ERROR · {entry.get('error', '')}"
        if event in {
            "master_compile_error",
            "master_program_error",
            "runtime_error",
            "runtime_interrupted",
        }:
            return f"ERROR   {entry.get('message', '')}"
        if event == "runtime_turn_budget_exhausted":
            return (
                "BUDGET  task turn limit reached · "
                f"{entry.get('turns_used', '?')}/{entry.get('max_turns', '?')}"
            )
        if event == "runtime_finished":
            return (
                "\n--- Final Result ---\n"
                f"Status  : {entry.get('phase', '?')}\n"
                f"Summary : {entry.get('summary', '')}\n"
                f"ResultRef: {entry.get('result_ref', '') or '—'}\n"
                f"Turns   : {entry.get('turns_used', '?')}/{entry.get('max_turns', '?')}\n"
                f"Elapsed : {float(entry.get('elapsed_s') or 0):.1f}s"
            )
        return ""

    def _trace(self, event: str, **payload: Any) -> None:
        trace = getattr(self, "trace", None)
        if trace is None:
            self.trace = []
            trace = self.trace
        payload = _redact_log_value(
            payload,
            getattr(self, "_access_log_redactions", ()),
        )
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
            and layer in {"worker", "observer", "action"}
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
                    "transform_completed",
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
            durable_data = _redact_log_value(
                data_store.private_dump(),
                getattr(self, "_access_log_redactions", ()),
            )
            (self.log_dir / "tool_agent_data_store.json").write_text(
                json.dumps(durable_data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def _write_artifacts(self, run: ToolAgentRun) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        redactions = getattr(self, "_access_log_redactions", ())
        durable_run = _redact_log_value(run.model_dump(mode="json"), redactions)
        durable_data = _redact_log_value(self.data_store.private_dump(), redactions)
        (self.log_dir / "tool_agent_trace.json").write_text(
            json.dumps(durable_run, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (self.log_dir / "tool_agent_data_store.json").write_text(
            json.dumps(durable_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


__all__ = ["ToolAgentRuntime", "ToolAgentRun"]
