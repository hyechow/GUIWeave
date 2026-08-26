"""Coding-Master runtime for dynamically orchestrated agentic Workers."""

from __future__ import annotations

import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Literal

from jsonschema import Draft202012Validator, validate
from langchain_core.messages import HumanMessage

from gui_agent.core.config import resolve_llm_config
from gui_agent.prompts import load_prompt_text
from gui_agent.core.runtime.action_settle import (
    VERIFY_TIMEOUT_S,
    has_snapped_point,
    settle_after_action,
)
from gui_agent.core.runtime.clock import (
    PlatformTimeSnapshot,
    host_time_fallback,
)
from gui_agent.core.schemas import (
    BaseAction,
    BaseActionDecision,
    TargetGrounding,
    TargetVerify,
)
from gui_agent.core.tool_agent.contracts import (
    CollectionRef,
    DynamicActionSpec,
    MaterializedFrame,
    ToolAgentRun,
    WorkerOutcome,
    WorkerSpec,
    WorkerStateSnapshot,
    WorkerStateTraceBatch,
    WorkerStrategy,
)
from gui_agent.core.tool_agent.action_guard import (
    action_boundary_error,
    assess_navigation_url,
    auth_codes_from_frame,
    auth_codes_from_text,
)
from gui_agent.core.tool_agent.action_geometry import control_at_point
from gui_agent.core.tool_agent.action_receipt import is_confirmed_selection_commit
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
    WorkerFrameTools,
    bind_actor_decision_transport,
    cacheable_system_message,
    diagnostic_prompt_reports,
    decode_actor_action,
    dynamic_actor_tools,
    dynamic_action_tool,
    generic_action_spec,
    frame_transition_message,
    image_message,
    input_binding_action,
    parse_json_object,
    response_usage,
    validate_dynamic_action_spec,
    validate_actor_tool_state,
    worker_frame_tools,
    worker_attempt_contract,
)
from gui_agent.core.tool_agent.replay import write_replay_artifact
from gui_agent.core.tool_agent.state_trace import (
    STATE_TRACE_OUTPUT_CONTRACT,
    latest_runtime_receipt,
    normalize_state_trace_payload,
    reduce_worker_state,
    state_actor_payload,
    state_continuation_payload,
)
from gui_agent.core.tool_agent.strategy import ReflectionResult, Reflector
from gui_agent.core.tool_agent.worker_memory import (
    WorkerJournal,
)
from gui_agent.core.vision.frame_analysis import visual_surface_fingerprint
from gui_agent.core.vision.target_verify import (
    ground_target,
    resolve_target_grounding,
    verify_target,
)
from llm.provider_config import (
    build_chat_model,
    chat_request_kwargs,
)

_MASTER_SYSTEM = load_prompt_text("task.tool_agent.master")
_STATE_SYSTEM = load_prompt_text("task.tool_agent.state")
_ACTOR_SYSTEM = load_prompt_text("task.tool_agent.actor")
_MAX_ACTION_GUARD_REPAIRS_PER_FRAME = 1
_MAX_PREDISPATCH_REPAIRS_PER_FRAME = 1
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
_TRANSIENT_MODEL_ERROR_NAMES = {
    "APIConnectionError", "APITimeoutError", "InternalServerError", "RateLimitError",
}


def _context_size_report(
    label: str,
    strategy: str,
    text: str,
    **extra: Any,
) -> dict[str, Any]:
    chars = len(text)
    return {
        "kind": "context_compression",
        "label": label,
        "strategy": strategy,
        "before_chars": chars,
        "after_chars": chars,
        "estimated_tokens": (chars + 3) // 4,
        "kept_tokens": (chars + 3) // 4,
        **extra,
    }


def _is_transient_model_error(error: BaseException) -> bool:
    """Recognize provider transport failures without coupling Runtime to one SDK."""
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if type(current).__name__ in _TRANSIENT_MODEL_ERROR_NAMES:
            return True
        current = current.__cause__ or current.__context__
    return False


def _scroll_boundary_feedback(directions: set[str]) -> dict[str, Any]:
    """Describe a mechanically observed traversal boundary without deciding completion."""
    return {
        "status": "collection_traversal_boundary",
        "boundary_directions": sorted(directions),
        "action_effect": "no_visual_change",
        "surface_transition": "none",
        "surface_continuity": "preserved",
        "decision_mode": "boundary_reconciliation",
        "decision_rule": (
            "Treat this as the boundary of only the current scroll container. Choose the "
            "next transition from goal-source alignment and accumulated coverage."
        ),
        "instruction": (
            "The collection cannot advance in the recorded boundary directions. Do not "
            "reverse merely to recheck handled records. "
            "The unchanged frame proves only that the same local surface remained active. "
            "It does not prove that this surface is the Goal Contract's required source, "
            "that another navigation path is invalid, or that task coverage is exhaustive. "
            "Navigate elsewhere when the current surface is not the required source. Reverse "
            "only when active progress names a specific unresolved target behind this boundary. "
            "Complete only when the current surface visibly establishes the required source "
            "and accumulated coverage is exhaustive."
        ),
    }


def _constrain_boundary_scroll_actions(
    actions: list[DynamicActionSpec], directions: set[str],
) -> list[DynamicActionSpec]:
    """Exclude only directions disproved within the current traversal episode."""
    axis = (
        ("up", "down") if directions.intersection({"up", "down"}) else
        ("left", "right") if directions.intersection({"left", "right"}) else ()
    )
    remaining = tuple(direction for direction in axis if direction not in directions)
    constrained: list[DynamicActionSpec] = []
    for action in actions:
        if action.capability != "scroll":
            constrained.append(action)
        elif len(remaining) == 1:
            payload = action.model_dump(mode="json")
            payload["fixed_args"] = {**payload.get("fixed_args", {}), "direction": remaining[0]}
            payload["exposed_args"] = [
                name for name in payload.get("exposed_args", []) if name != "direction"
            ]
            constrained.append(DynamicActionSpec.model_validate(payload))
        elif remaining:
            constrained.append(action)
    return constrained


def _update_traversal_boundaries(boundaries: set[str], receipt: Any) -> None:
    """Persist boundary directions across scrolls; another capability ends the episode."""
    if receipt is None:
        return
    if receipt.tool == "scroll":
        if receipt.outcome.kind == "no_effect":
            direction = str(receipt.args.get("direction") or "")
            if direction:
                boundaries.add(direction)
    elif receipt.executed:
        boundaries.clear()


class _RuntimeCancelled(Exception):
    """Internal cooperative stop raised only at safe runtime boundaries."""


class _WorkerActionRejected(ValueError):
    """An action contract was rejected before any platform input was dispatched."""


class _EachExhausted(Exception):
    """A consume="each" array cursor passed the end of its plan array.

    Raised while materializing an each binding so the runtime drops that action
    from the available set instead of failing the Worker.
    """


_ACTION_TYPES = {"open_url": "navigate"}


_REDACTED_ACCESS_VALUE = "[session access value redacted]"
_WORKER_VERIFY_POOL = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="tool-worker-verify",
)
_TARGET_VERIFIED_ACTION_TYPES = {
    "tap", "click", "type", "long_press", "select_option",
}
_BATCH_FINAL_CAPABILITIES = {
    "ask_user", "back", "home", "app_switch", "launch_app", "open_url", "scroll", "drag",
}
_STATE_TARGET_CAPABILITIES = {
    "tap", "click", "type", "drag", "long_press", "select_option",
}


def _worker_action_error(exc: Exception) -> dict[str, Any]:
    payload = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    if isinstance(exc, _WorkerActionRejected):
        payload["reuse_current_frame"] = True
    return payload


def _state_target_binding_error(
    state: WorkerStateSnapshot,
    capability: str,
    state_target_ref: str | None,
) -> str:
    """Validate Actor's semantic target without moving geometry into State."""

    if capability not in _STATE_TARGET_CAPABILITIES:
        return ""
    payload = state_actor_payload(state)
    frontier = payload["visible_targets"]["unresolved_frontier"]
    if state_target_ref == "":
        if frontier:
            return (
                "this spatial action must copy the exact state_target_ref from the "
                "current unresolved_frontier, or explicitly use null for an interface "
                "control; x/y still come from the screenshot"
            )
        return ""
    if state_target_ref is None:
        return ""
    target = next(
        (item for item in frontier if item["target_ref"] == state_target_ref),
        None,
    )
    if target is None:
        resolved = payload["visible_targets"]["resolved_refs_do_not_repeat"]
        if state_target_ref in resolved or state_target_ref in payload["resolved_target_refs"]:
            return (
                f"state_target_ref {state_target_ref!r} is already resolved and must not "
                "be activated again"
            )
        return (
            f"state_target_ref {state_target_ref!r} is not in the current visible "
            "unresolved_frontier"
        )
    if target["owned_region_visibility"] != "unobscured":
        return (
            f"state_target_ref {state_target_ref!r} is only an edge fragment; reposition "
            "it before spatial activation"
        )
    return ""


def _action_feedback(items: object, action_type: str) -> list[dict[str, Any]]:
    """Normalize feedback without attributing arbitrary background HTTP failures."""
    if action_type not in {"navigate", "tap", "press_enter"}:
        return []
    feedback: list[dict[str, Any]] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        status = item.get("status")
        kind = str(item.get("kind") or "")
        body = str(item.get("body") or "").strip()
        try:
            decoded = json.loads(body) if body else None
        except json.JSONDecodeError:
            decoded = None
        message = (
            decoded.get("message") or decoded.get("error_message")
            if isinstance(decoded, dict) else ""
        )
        rejected = bool(
            isinstance(decoded, dict)
            and (decoded.get("error") is True or decoded.get("success") is False)
        )
        http_error = isinstance(status, (int, float)) and int(status) >= 400
        native_navigation_failure = kind == "navigation" and (
            rejected or http_error or bool(message)
        )
        causally_rejected = native_navigation_failure
        if message or rejected or http_error:
            feedback.append({
                "status": int(status or 0),
                "url": str(item.get("url") or ""),
                "rejected": causally_rejected,
                "message": str(message or body[:500]).strip(),
            })
    return feedback


def _vision_future_result(future: Any, schema: Any) -> tuple[Any | None, Exception | None]:
    try:
        return schema.model_validate(future.result(timeout=VERIFY_TIMEOUT_S)), None
    except Exception as exc:  # noqa: BLE001 - optional vision checks fail open
        future.cancel()
        return None, exc


def _target_verification_result(future: Any) -> tuple[dict[str, Any] | None, Exception | None]:
    verification, error = _vision_future_result(future, TargetVerify)
    if error is not None:
        return None, error
    return {
        "status": "on_target" if verification.on_target else "off_target",
        "actual_element": verification.actual_element,
        "reason": verification.reason,
    }, None


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


def _llm(config_name: str) -> tuple[Any, Any]:
    cfg = resolve_llm_config(config_name)
    return build_chat_model(cfg), cfg


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
        if max_compile_attempts < 1:
            raise ValueError("max_compile_attempts must be positive")
        self.max_turns = max_turns
        self.max_compile_attempts = max_compile_attempts
        self.allow_multi_action = allow_multi_action
        read_time = getattr(bundle, "read_time", None)
        try:
            captured_time = read_time(platform) if callable(read_time) else None
            self.platform_time = PlatformTimeSnapshot.model_validate(captured_time)
        except Exception as exc:  # noqa: BLE001 - provenance makes fallback explicit
            self.platform_time = host_time_fallback(
                bundle.platform,
                reason=f"platform adapter clock unavailable: {type(exc).__name__}",
            )
        if not callable(read_time):
            self.platform_time = host_time_fallback(
                bundle.platform,
                reason="platform adapter does not expose a clock reader",
        )
        self.data_store = RuntimeDataStore()
        self.master, self.master_cfg = _llm("tool_agent.master")
        self.worker, self.worker_cfg = _llm("tool_agent.worker")
        self._master_explicit_cache = _supports_explicit_prompt_cache(self.master_cfg)
        self.reflector = Reflector(
            self.worker,
            generation_model_name=self.worker_cfg.model,
            explicit_cache=_supports_explicit_prompt_cache(self.worker_cfg),
        )
        self._worker_explicit_cache = _supports_explicit_prompt_cache(self.worker_cfg)
        self.materializer = PerceptionMaterializer(
            mode=perception_mode,
            data_store=self.data_store,
            log_dir=log_dir,
            platform_time=self.platform_time,
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
        self._worker_state_snapshots: dict[str, WorkerStateSnapshot] = {}
        self._worker_state_frames: dict[str, tuple[str, bytes]] = {}
        # Per-worker, per-array-ref cursors for consume="each" input bindings:
        # (worker_id, ref_name) -> next array index to consume.
        self._each_cursors: dict[tuple[str, str], int] = {}
        self._master_knowledge = ""
        self._worker_knowledge = ""
        self._installed_app_names: tuple[str, ...] | None = None
        self._executor = bundle.make_executor(platform)
        self._target_ground_pool = self._target_verify_pool = _WORKER_VERIFY_POOL
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
        worker_knowledge: str = "",
        access_context: str = "",
        page_url: str = "",
        page_title: str = "",
    ) -> ToolAgentRun:
        self._worker_access_context = access_context.strip()
        self._master_knowledge = knowledge
        self._worker_knowledge = worker_knowledge or knowledge
        self._task_goal = goal
        self._start_page_url = page_url.strip()
        self.materializer.task_goal = goal
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
            self._worker_knowledge = ""
            self._task_goal = ""
            self._start_page_url = ""
            self.materializer.task_goal = ""
            self._access_log_redactions = ()
            if executor is not None:
                setattr(executor, "sensitive_text_values", ())
            journals = getattr(self, "_worker_journals", None)
            if journals is not None:
                journals.clear()
            last_frames = getattr(self, "_worker_last_frames", None)
            if last_frames is not None:
                last_frames.clear()
            for name in (
                "_each_cursors", "_worker_state_snapshots", "_worker_state_frames",
            ):
                state = getattr(self, name, None)
                if isinstance(state, dict):
                    state.clear()
            self._active_worker_id = ""

    def _run(self, goal: str, *, knowledge: str = "", page_url: str = "", page_title: str = "") -> ToolAgentRun:
        if getattr(self, "platform_time", None) is None:
            platform = getattr(self, "platform", None) or getattr(
                getattr(self, "bundle", None),
                "platform",
                "browser",
            )
            self.platform_time = host_time_fallback(
                platform,
                reason="runtime restored without a captured platform clock",
            )
        self._trace(
            "runtime_started",
            goal=goal,
            perception_mode=self.perception_mode,
            max_turns=int(getattr(self, "max_turns", 50)),
            multi_action=bool(getattr(self, "allow_multi_action", False)),
            master_model=self.master_cfg.model,
            worker_model=self.worker_cfg.model,
            platform_time=self.platform_time.model_dump(mode="json"),
        )
        task_context = {
            "goal": goal,
            "page": {"url": page_url, "title": page_title},
            "task_reference_time": self.platform_time.model_dump(mode="json"),
            "relative_date_offsets": self.platform_time.relative_date_offsets(),
            "platform": self._platform_prompt_context(),
            "application_knowledge": knowledge or "(none)",
        }
        final_ref = None
        final_summary = ""
        final_effect: Literal["mutation", "data", "ui_state", "none"] = "none"
        phase: Literal["completed", "failed"] = "failed"
        orchestration = WorkerOrchestrationContext(
            data_store=self.data_store,
            run_gui_worker=self._run_logical_worker,
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
            platform_time=self.platform_time.model_dump(mode="json"),
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
    def _reflected_worker_id(worker_id: str, attempt_no: int) -> str:
        suffix = f"_reflection_{attempt_no}"
        if len(worker_id) + len(suffix) <= 64:
            return worker_id + suffix
        digest = hashlib.sha256(worker_id.encode()).hexdigest()[:8]
        prefix_size = 64 - len(suffix) - len(digest) - 1
        return f"{worker_id[:prefix_size]}_{digest}{suffix}"

    @staticmethod
    def _inherit_task_memory(
        journals: dict[str, WorkerJournal],
        journal: WorkerJournal,
        worker_id: str,
    ) -> None:
        """Carry only Runtime-authored task facts into a reflected attempt."""

        if "_reflection_" not in worker_id:
            return
        base_id = worker_id.split("_reflection_", 1)[0]
        base = journals.get(base_id)
        if base is None or journal.events:
            return
        for event in base.events:
            if (
                event.kind == "memory_update"
                and event.lifetime == "task"
                and event.origin == "runtime"
                and event.event_ref not in {item.event_ref for item in journal.events}
            ):
                journal._append(replace(event, attempt_id=worker_id))

    def _preserve_progress_for_reflected_attempt(
        self,
        *,
        current_worker_id: str,
        next_worker_id: str,
    ) -> None:
        """Alias one logical Worker's Journal across approach revisions."""
        journals = getattr(self, "_worker_journals", None)
        if journals is not None and current_worker_id in journals:
            journals[next_worker_id] = journals[current_worker_id]
        last_frames = getattr(self, "_worker_last_frames", {})
        if current_worker_id in last_frames:
            last_frames[next_worker_id] = last_frames[current_worker_id]
        contexts = getattr(self, "_worker_last_contexts", {})
        if current_worker_id in contexts:
            contexts[next_worker_id] = contexts[current_worker_id]

    def _worker_recovery_experience(
        self,
        worker_id: str,
    ) -> dict[str, Any]:
        """Reuse the exact bounded context last projected to the Worker."""

        return dict(getattr(self, "_worker_last_contexts", {}).get(worker_id) or {})

    def _request_reflection(
        self,
        *,
        logical_worker_id: str,
        prior_worker_id: str,
        original_spec: WorkerSpec,
        prior_outcome: WorkerOutcome,
        failure_reason: str,
        attempt_no: int,
        attempted_approaches: list[WorkerStrategy],
    ) -> ReflectionResult:
        context = {
            "logical_worker_id": logical_worker_id,
            "prior_worker_id": prior_worker_id,
            "reflection_attempt": attempt_no,
            "failure_reason": failure_reason,
            "goal_contract": original_spec.model_dump(mode="json"),
            "prior_approach": original_spec.strategy.approach,
            "prior_outcome": prior_outcome.model_dump(mode="json"),
            "application_knowledge": getattr(self, "_master_knowledge", "") or "(none)",
            "platform": self._platform_prompt_context(),
            **self._worker_recovery_experience(prior_worker_id),
            "failure": prior_outcome.model_dump(mode="json"),
            "attempted_approaches": [
                item.model_dump(mode="json") for item in attempted_approaches
            ],
            "supported_capabilities": sorted(self._platform_capabilities),
            "installed_applications": list(self._installed_applications()),
        }
        def trace(event: str, **payload: Any) -> None:
            self._trace(
                event,
                logical_worker_id=logical_worker_id,
                prior_worker_id=prior_worker_id,
                reflection_attempt=attempt_no,
                **payload,
            )

        return self.reflector.reflect(
            context=context,
            original_strategy=original_spec.strategy,
            on_event=trace,
        )

    def _run_logical_worker(
        self,
        worker_id: str,
        spec: WorkerSpec,
    ) -> WorkerOutcome:
        if self._turn_budget_exhausted():
            return self._turn_budget_failure(worker_id=worker_id, steps=0)
        current_id = worker_id
        current_spec = spec
        attempted_approaches = [spec.strategy]
        consumed_steps = 0
        reflection_attempt = 0
        while True:
            outcome = self._run_worker(
                current_id,
                current_spec,
                require_attempt=reflection_attempt > 0,
            )
            consumed_steps += outcome.steps
            if self._turn_budget_exhausted() and outcome.phase == "failed":
                if outcome.failure_kind == "budget_exhausted":
                    return self._turn_budget_failure(
                        worker_id=current_id,
                        steps=consumed_steps,
                    )
                return outcome.model_copy(update={"steps": consumed_steps})
            route = Reflector.route(outcome)
            if route in {"complete", "master", "abort"}:
                return outcome.model_copy(update={"steps": consumed_steps})
            if route != "replace":
                raise AssertionError(f"unexpected Worker failure route {route!r}")
            failure_reason = (
                "The Worker produced a verified empty filtered collection."
                if outcome.phase == "completed"
                else f"The Worker failed to satisfy the subgoal: {outcome.summary}"
            )
            self._trace(
                "reflection_requested",
                logical_worker_id=worker_id,
                worker_id=current_id,
                reflection_attempt=reflection_attempt,
                reason=failure_reason,
                strategy=current_spec.strategy.model_dump(mode="json"),
                outcome=outcome.model_dump(mode="json"),
            )
            try:
                reflected = self._request_reflection(
                    logical_worker_id=worker_id,
                    prior_worker_id=current_id,
                    original_spec=current_spec,
                    prior_outcome=outcome,
                    failure_reason=failure_reason,
                    attempt_no=reflection_attempt + 1,
                    attempted_approaches=attempted_approaches,
                )
            except Exception as exc:  # noqa: BLE001 - becomes typed Worker failure
                return WorkerOutcome(
                    phase="failed",
                    summary=f"Reflection failed: {type(exc).__name__}: {exc}",
                    failure_kind="generator_invalid",
                    steps=consumed_steps,
                )
            revised = reflected.strategy
            selection_reason = reflected.reason
            reflection_attempt += 1
            if reflected.decision in {"resume", "reconcile_state"}:
                self._trace(
                    "reflection_resumed",
                    logical_worker_id=worker_id,
                    worker_id=current_id,
                    reflection_attempt=reflection_attempt,
                    decision=reflected.decision,
                    reason=selection_reason,
                )
                continue
            if reflected.decision == "escalate_to_master":
                self._trace(
                    "reflection_escalated",
                    logical_worker_id=worker_id,
                    worker_id=current_id,
                    reflection_attempt=reflection_attempt,
                    reason=selection_reason,
                )
                return outcome.model_copy(update={
                    "summary": f"{outcome.summary.rstrip('.')} - escalation: {selection_reason}",
                    "steps": consumed_steps,
                })
            if reflected.decision == "stop" or revised is None:
                self._trace(
                    "reflection_stopped",
                    logical_worker_id=worker_id,
                    worker_id=current_id,
                    reflection_attempt=reflection_attempt,
                    reason=selection_reason,
                )
                return outcome.model_copy(update={
                    "summary": (
                        f"{outcome.summary.rstrip('.')} - Reflector stopped: "
                        f"{selection_reason}"
                    ),
                    "steps": consumed_steps,
                })
            attempted_approaches.append(revised)
            next_id = self._reflected_worker_id(worker_id, reflection_attempt)
            self._trace(
                "reflected_worker_dispatched",
                logical_worker_id=worker_id,
                prior_worker_id=current_id,
                worker_id=next_id,
                reflection_attempt=reflection_attempt,
                prior_outcome=outcome.model_dump(mode="json"),
                selection_reason=selection_reason,
                strategy=revised.model_dump(mode="json"),
            )
            self._preserve_progress_for_reflected_attempt(
                current_worker_id=current_id, next_worker_id=next_id,
            )
            current_id = next_id
            current_spec = current_spec.model_copy(update={"strategy": revised})

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

    def _active_worker_journal(self) -> WorkerJournal | None:
        worker_id = str(getattr(self, "_active_worker_id", "") or "")
        return (getattr(self, "_worker_journals", None) or {}).get(worker_id)

    def _run_worker(
        self,
        worker_id: str,
        spec: WorkerSpec,
        *,
        require_attempt: bool = False,
    ) -> WorkerOutcome:
        self._validate_worker_spec(spec)
        self._active_worker_id = worker_id
        journals = getattr(self, "_worker_journals", None)
        if journals is None:
            self._worker_journals = {}
            journals = self._worker_journals
        snapshots = getattr(self, "_worker_state_snapshots", None)
        if snapshots is None:
            self._worker_state_snapshots = {}
            snapshots = self._worker_state_snapshots
        state_frames = getattr(self, "_worker_state_frames", None)
        if state_frames is None:
            self._worker_state_frames = {}
            state_frames = self._worker_state_frames
        journal = journals.setdefault(worker_id, WorkerJournal(worker_id=worker_id))
        self._inherit_task_memory(journals, journal, worker_id)
        retained_events = len(journal.events)
        self._trace(
            "worker_started",
            worker_id=worker_id,
            retained_memory_events=retained_events,
            profile=spec.profile,
            goal=spec.goal,
            success_criteria=spec.success_criteria,
            requirement_ids=[item.id for item in spec.data_requirements],
            strategy=spec.strategy.approach,
        )
        active_actions = self._initial_worker_actions(spec)
        observed_auth_codes = {
            code
            for text in (
                getattr(self, "_task_goal", ""), spec.goal,
                getattr(self, "_master_knowledge", ""),
                getattr(self, "_worker_access_context", ""),
            )
            for code in auth_codes_from_text(str(text or ""))
        }
        step = 0
        reusable_observation: tuple[
            MaterializedFrame,
            bytes,
            dict[str, Any],
        ] | None = None
        predispatch_repair_turn = 0
        traversal_boundaries: set[str] = set()
        while True:
            self._raise_if_cancelled()
            if reusable_observation is None:
                if self._turn_budget_exhausted():
                    return self._turn_budget_failure(
                        worker_id=worker_id,
                        steps=step,
                    )
                frame, png = self._observe(spec)
                step += 1
                observed_auth_codes.update(auth_codes_from_frame(frame))
                # Screenshot-only delivery surfaces preserve codes through journal facts.
                observed_auth_codes.update(auth_codes_from_text(" ".join(
                    journal.active_fact_statements(frame_id=frame.frame_id)
                )))
                initial_same_frame_feedback = None
                predispatch_repair_turn = 0
            else:
                frame, png, initial_same_frame_feedback = reusable_observation
                reusable_observation = None
            last_frames = getattr(self, "_worker_last_frames", None)
            if last_frames is None:
                self._worker_last_frames = {}
                last_frames = self._worker_last_frames
            last_frames[worker_id] = frame
            active_actions = self._refresh_each_actions(spec, active_actions)
            frame_assessment = worker_frame_tools(
                spec,
                active_actions,
                frame,
                attempted_action=bool(journal.executed_tools),
            )
            prior_receipt = journal.latest_action_receipt
            _update_traversal_boundaries(traversal_boundaries, prior_receipt)
            if initial_same_frame_feedback is None and traversal_boundaries:
                initial_same_frame_feedback = _scroll_boundary_feedback(
                    traversal_boundaries
                )
            frame_actions = frame_assessment.allowed_actions
            if (
                initial_same_frame_feedback is not None
                and initial_same_frame_feedback.get("status")
                == "collection_traversal_boundary"
            ):
                frame_actions = _constrain_boundary_scroll_actions(
                    frame_actions, traversal_boundaries,
                )
                frame_assessment = WorkerFrameTools(
                    frame_actions, frame_assessment.completion_mode,
                )
            worker_tools = self._worker_tools_for_frame(
                spec,
                frame_actions,
                frame,
                assessment=frame_assessment,
                allow_failure=not require_attempt or bool(journal.executed_tools),
            )
            action_limit = self._worker_action_limit()
            action_protocol = str(
                getattr(getattr(self, "worker_cfg", None), "action_protocol", "tool_call")
            )
            guard_repair_turn = 0
            same_frame_feedback = initial_same_frame_feedback
            prior_state = snapshots.get(worker_id)
            if prior_state is not None and prior_state.frame_id == frame.frame_id:
                state = prior_state
                self._trace(
                    "worker_state_reused",
                    step=step,
                    frame_id=frame.frame_id,
                    reason="same_frame_action_repair",
                )
            else:
                state_messages, state_context_reports = self._state_messages(
                    spec=spec,
                    journal=journal,
                    frame=frame,
                    png=png,
                    same_frame_feedback=same_frame_feedback,
                )
                try:
                    state = self._invoke_state_role(
                        step=step,
                        spec=spec,
                        journal=journal,
                        frame=frame,
                        messages=state_messages,
                        context_reports=state_context_reports,
                    )
                except Exception as exc:  # noqa: BLE001 - typed State is required
                    return WorkerOutcome(
                        phase="failed",
                        summary=f"State role failed on the current frame: {exc}",
                        failure_kind="protocol_invalid",
                        steps=step - 1,
                    )
                snapshots[worker_id] = state
                state_frames[worker_id] = (frame.frame_id, png)
            observed_auth_codes.update(
                code
                for text in state.fact_statements
                for code in auth_codes_from_text(text)
            )
            while True:
                messages, context_reports = self._actor_messages(
                    spec=spec,
                    journal=journal,
                    state=state,
                    frame=frame,
                    png=png,
                    same_frame_feedback=same_frame_feedback,
                )
                response = None
                call = None
                calls: list[dict[str, Any]] = []
                protocol_schema = (
                    f"{action_protocol}: Actor ordered actions"
                    if getattr(self, "allow_multi_action", False)
                    else f"{action_protocol}: Actor exactly one action"
                )
                llm_elapsed_s = 0.0
                token_usage: dict[str, int] = {}
                request_kwargs = chat_request_kwargs(
                    getattr(getattr(self, "worker_cfg", None), "model", None)
                )
                transport = bind_actor_decision_transport(
                    self.worker,
                    worker_tools,
                    protocol=action_protocol,
                    bind_kwargs=request_kwargs,
                )
                bound_worker, decision_instruction, transport_repair = transport
                if decision_instruction:
                    messages.append(HumanMessage(content=decision_instruction))
                for attempt in range(2):
                    started_at = time.perf_counter()
                    for transport_attempt in range(2):
                        try:
                            response = bound_worker.invoke(messages)
                            break
                        except Exception as exc:  # noqa: BLE001 - provider-neutral retry
                            if transport_attempt or not _is_transient_model_error(exc):
                                raise
                            self._trace(
                                "worker_model_retry",
                                step=step,
                                frame_id=frame.frame_id,
                                error=f"{type(exc).__name__}: {exc}",
                            )
                    llm_elapsed_s += time.perf_counter() - started_at
                    token_usage = response_usage(response)
                    try:
                        call, raw_state, calls = decode_actor_action(
                            response,
                            protocol=action_protocol,
                        )
                        if call["name"] == "continue_with_actions":
                            self._validate_multi_action_calls(
                                calls,
                                frame_actions,
                                max_actions=action_limit,
                            )
                        if raw_state is not None:
                            raise ProtocolError(
                                "Actor must not emit state; State already ran on this frame"
                            )
                        validate_actor_tool_state(call["name"], state.status)
                        self._accumulate_observed_rows(spec, worker_id, call, step)
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
                            return WorkerOutcome(
                                phase="failed",
                                summary=(
                                    "Worker repeated an invalid action protocol on the same "
                                    f"frame: {exc}"
                                ),
                                failure_kind="protocol_invalid",
                                steps=step - 1,
                            )
                        repair_message = HumanMessage(content=(
                            "Protocol repair: the previous response was invalid. On this SAME frame, "
                            f"the Runtime reported: {exc}. {transport_repair.capitalize()} "
                            "Do not emit a state field. "
                            + "If continue_with_actions is used, include "
                            f"between 1 and {action_limit} executable actions. Only use a "
                            "terminal tool when Runtime exposed that tool on this frame. "
                            "No action was executed."
                        ))
                        messages.extend([response, repair_message])
                assert response is not None and call is not None
                for action_call in calls:
                    action_call["_state_target_ref"] = action_call["args"].pop(
                        "state_target_ref", "",
                    )
                self._trace(
                    "worker_decision",
                    step=step,
                    frame_id=frame.frame_id,
                    profile=spec.profile,
                    state=state.model_dump(mode="json"),
                    tool=call["name"],
                    args=call["args"],
                    state_target_ref=(
                        calls[0].get("_state_target_ref") if calls else None
                    ),
                    llm_elapsed_s=round(llm_elapsed_s, 3),
                    token_usage=token_usage,
                    context_reports=[*context_reports, *diagnostic_prompt_reports(
                        "tool_agent.actor",
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
                    replay_context={
                        "version": 2,
                        "worker_spec": spec.model_dump(mode="json"),
                        "actions": [
                            action.model_dump(mode="json")
                            for action in frame_actions
                        ],
                        "executed_tools": sorted(journal.executed_tools),
                        "active_commitment_refs": list(journal.active_commitment_refs),
                        "enhanced": getattr(self, "perception_mode", "vision-only")
                        == "enhanced",
                        "multi_action": bool(getattr(self, "allow_multi_action", False)),
                    },
                )
                if (
                    call["name"] == "complete"
                    and spec.profile == "operator"
                ):
                    commit_controls = self._visible_commit_controls(frame, state)
                    if commit_controls:
                        reason = (
                            "State claimed completion while a cited enabled commit "
                            "control remains visible"
                        )
                        self._trace(
                            "worker_completion_recheck",
                            step=step,
                            frame_id=frame.frame_id,
                            controls=commit_controls,
                        )
                        journal.record_guard(
                            step=step,
                            repair_turn=1,
                            tool="complete",
                            reason=reason,
                        )
                        return WorkerOutcome(
                            phase="failed",
                            summary=reason,
                            failure_kind="protocol_invalid",
                            steps=step - 1,
                        )
                guarded_call = calls[0] if call["name"] == "continue_with_actions" else call
                action_spec = next(
                    (item for item in frame_actions if item.name == guarded_call["name"]),
                    None,
                )
                if action_spec is not None:
                    observed_auth_codes.update(
                        code
                        for text in state.fact_statements
                        for code in auth_codes_from_text(text)
                    )
                    resolved_guard_args = {
                        "description": action_spec.description,
                        **action_spec.fixed_args,
                        **guarded_call["args"],
                    }
                    blocked_reason = _state_target_binding_error(
                        state,
                        action_spec.capability,
                        guarded_call.get("_state_target_ref", ""),
                    )
                    if not blocked_reason:
                        blocked_reason = action_boundary_error(
                            action_spec.capability,
                            resolved_guard_args,
                            frame,
                            observed_auth_codes,
                        )
                    if blocked_reason:
                        if guard_repair_turn >= _MAX_ACTION_GUARD_REPAIRS_PER_FRAME:
                            return WorkerOutcome(
                                phase="failed",
                                summary=blocked_reason,
                                failure_kind="action_contract_invalid",
                                steps=step - 1,
                            )
                        guard_repair_turn += 1
                        feedback = {
                            "status": "action_boundary_invalid",
                            "reason": blocked_reason,
                            "instruction": (
                                "Correct the action so it satisfies the current frame's "
                                "explicit action contract."
                            ),
                        }
                        self._trace(
                            "worker_action_rejected",
                            step=step,
                            frame_id=frame.frame_id,
                            tool=guarded_call["name"],
                            args=guarded_call["args"],
                            reason=blocked_reason,
                        )
                        journal.record_guard(
                            step=step,
                            repair_turn=guard_repair_turn,
                            tool=guarded_call["name"],
                            reason=blocked_reason,
                        )
                        same_frame_feedback = feedback
                        continue
                break
            try:
                if call["name"] == "continue_with_actions":
                    result_payload, terminal = self._execute_multi_action_calls(
                        worker_id=worker_id,
                        spec=spec,
                        actions=frame_actions,
                        calls=calls,
                        state=state,
                        step=step,
                        frame=frame,
                        png=png,
                        journal=journal,
                        commitment_refs=(),
                        observed_auth_codes=observed_auth_codes,
                    )
                else:
                    result_payload, terminal = self._execute_worker_tool(
                        spec,
                        frame_actions,
                        call,
                        png,
                        frame,
                        worker_id=worker_id,
                    )
            except Exception as exc:  # noqa: BLE001 - feed capability failure back into ReAct
                result_payload = _worker_action_error(exc)
                terminal = None
                self._trace("worker_tool_error", step=step, tool=call["name"], error=result_payload["error"])
            if call["name"] != "continue_with_actions":
                journal.record_action_result(
                    step=step,
                    frame_id=frame.frame_id,
                    tool=call["name"],
                    args=call["args"],
                    result=result_payload,
                    commitment_refs=(),
                )
                journal.record_runtime_result(
                    step=step,
                    result=result_payload,
                )
            if result_payload.get("reuse_current_frame") is True:
                predispatch_repair_turn += 1
                if predispatch_repair_turn > _MAX_PREDISPATCH_REPAIRS_PER_FRAME:
                    return WorkerOutcome(
                        phase="failed",
                        summary=(
                            "Worker repeated an action rejected before dispatch after "
                            "same-frame corrective feedback."
                        ),
                        failure_kind="action_contract_invalid",
                        steps=step,
                    )
                error = str(
                    result_payload.get("error")
                    or result_payload.get("reason")
                    or "The action contract was rejected before dispatch."
                )
                reusable_observation = (frame, png, {
                    "status": "rejected_before_dispatch",
                    "error": error,
                    "instruction": (
                        "No GUI action was executed. Correct the action using this same "
                        "screenshot; do not request another observation."
                    ),
                })
                self._trace(
                    "worker_same_frame_action_repair",
                    step=step,
                    frame_id=frame.frame_id,
                    repair_turn=predispatch_repair_turn,
                    error=error,
                )
                continue
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
            if terminal == "each_next":
                # A consume="each" operator finished one plan element; the cursor
                # was advanced inside _execute_worker_tool. State is scoped to that
                # element, so the next frame must initialize a fresh snapshot.
                snapshots = getattr(self, "_worker_state_snapshots", None)
                if isinstance(snapshots, dict):
                    snapshots.pop(worker_id, None)
                state_frames = getattr(self, "_worker_state_frames", None)
                if isinstance(state_frames, dict):
                    state_frames.pop(worker_id, None)
                self._trace(
                    "worker_each_advanced",
                    step=step,
                    worker_id=worker_id,
                    profile=spec.profile,
                )
                continue
            if terminal == "report_blocked":
                reason = FailWorkerArgs.model_validate(call["args"]).reason
                return WorkerOutcome(
                    phase="failed",
                    summary=reason,
                    failure_kind="worker_blocked",
                    steps=step,
                )
            if terminal in {"platform_rejected", "navigation_blocked"}:
                reason = str(
                    result_payload.get("reason")
                    or "The platform rejected the requested action."
                )
                self._trace(
                    f"worker_{terminal}",
                    step=step,
                    profile=spec.profile,
                    reason=reason,
                    platform_feedback=result_payload.get("platform_feedback") or [],
                )
                return WorkerOutcome(
                    phase="failed",
                    summary=reason,
                    failure_kind=terminal,
                    steps=step,
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
            failure_kind="budget_exhausted",
            steps=steps,
        )

    def _observe(self, spec: WorkerSpec) -> tuple[MaterializedFrame, bytes]:
        observe_started_at = time.perf_counter()
        journal = self._active_worker_journal()
        logical_worker_id = journal.worker_id if journal is not None else ""
        executed_tools = getattr(journal, "executed_tools", set())
        frame_no = self._frame_no + 1

        frame, png = self.materializer.observe(
            bundle=self.bundle,
            platform=self.platform,
            requirements=spec.data_requirements,
            allow_linked_details=not any(
                binding.name not in executed_tools
                for binding in spec.input_bindings
            ),
            state_scope=logical_worker_id,
            frame_no=frame_no,
        )
        self._frame_no = frame_no
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
            readiness=frame.readiness,
            platform_time=frame.platform_time,
            # Absence of a structured-control inventory is not evidence that the
            # visible surface has zero controls. Android native observation is
            # screenshot-only, so omit this signal instead of manufacturing a
            # misleading count.
            **({"control_count": len(frame.controls)} if frame.controls else {}),
            observe_seconds=round(time.perf_counter() - observe_started_at, 3),
            capture_timing=getattr(self.platform, "last_capture_timing", None),
        )
        durable_frame = _redact_log_value(
            frame.model_dump(mode="json"),
            getattr(self, "_access_log_redactions", ()),
        )
        (self.log_dir / f"observation_tool_agent_{self._frame_no}.json").write_text(
            json.dumps(durable_frame, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        return frame, png

    def _state_system_prompt(self) -> str:
        return self._role_system_prompt(
            _STATE_SYSTEM,
            include_access=False,
            include_apps=False,
            include_knowledge=True,
        )

    def _actor_system_prompt(self) -> str:
        return self._role_system_prompt(
            _ACTOR_SYSTEM,
            include_access=True,
            include_apps=True,
            include_knowledge=True,
        )

    def _role_system_prompt(
        self,
        base: str,
        *,
        include_access: bool,
        include_apps: bool,
        include_knowledge: bool = False,
    ) -> str:
        # Keep reusable role instructions and deployment context cacheable.
        prompt = base
        installed_apps = self._installed_applications() if include_apps else ()
        if installed_apps:
            prompt += (
                "\n\n## Installed applications\n"
                "Use only these exact Runtime-provided names with launch_app: "
                + json.dumps(installed_apps, ensure_ascii=False)
            )
        knowledge = getattr(
            self, "_worker_knowledge", getattr(self, "_master_knowledge", ""),
        ).strip()
        if include_knowledge and knowledge:
            prompt += (
                "\n\n## Application knowledge (interface mechanics only)\n"
                + knowledge
            )
        access_context = getattr(self, "_worker_access_context", "")
        if include_access and access_context:
            prompt += (
                "\n\n## Session access context (private runtime input)\n"
                "Use these deployment/access facts only when the authoritative State and current "
                "screenshot require authentication. Never repeat credentials in tool descriptions, "
                "final results, or user-facing text.\n"
                + access_context
            )
        return prompt

    def _state_messages(
        self,
        *,
        spec: WorkerSpec,
        journal: WorkerJournal,
        frame: MaterializedFrame,
        png: bytes,
        same_frame_feedback: dict[str, Any] | None,
    ) -> tuple[list[Any], list[dict[str, Any]]]:
        """Build one small init/append observation call for the current frame."""
        snapshots = getattr(self, "_worker_state_snapshots", {})
        previous = snapshots.get(journal.worker_id)
        mode = "append" if isinstance(previous, WorkerStateSnapshot) else "init"
        previous_view = (
            state_continuation_payload(previous)
            if isinstance(previous, WorkerStateSnapshot)
            else None
        )
        current_element = self._current_each_element(spec, journal.worker_id)
        state_frames = getattr(self, "_worker_state_frames", {})
        previous_frame = state_frames.get(journal.worker_id)
        has_previous_frame = bool(
            isinstance(previous, WorkerStateSnapshot)
            and isinstance(previous_frame, tuple)
            and len(previous_frame) == 2
            and previous_frame[0] == previous.frame_id
            and previous.frame_id != frame.frame_id
        )
        state_payload = {
            "goal_contract": {
                "goal": spec.goal,
                "success_criteria": {
                    f"criterion_{index}": statement
                    for index, statement in enumerate(spec.success_criteria, start=1)
                },
            },
            "mode": mode,
            "frame_id": frame.frame_id,
            "output_contract": STATE_TRACE_OUTPUT_CONTRACT,
        }
        if isinstance(previous, WorkerStateSnapshot):
            state_payload["visual_transition"] = {
                "previous_frame_id": previous.frame_id,
                "current_frame_id": frame.frame_id,
                "previous_frame_available": has_previous_frame,
            }
            state_payload["previous_state"] = previous_view
        if current_element is not None:
            state_payload["current_element"] = current_element
        receipt = latest_runtime_receipt(journal)
        if receipt is not None:
            state_payload["latest_runtime_receipt"] = receipt
        if same_frame_feedback:
            state_payload["same_frame_runtime_feedback"] = same_frame_feedback
        state_input = json.dumps(state_payload, ensure_ascii=False)
        current_scale = float(
            getattr(getattr(self, "worker_cfg", None), "image_scale", 1.0)
        )
        if has_previous_frame:
            assert previous_frame is not None
            state_message = frame_transition_message(
                state_input,
                previous_frame[1],
                png,
                previous_frame_id=previous_frame[0],
                current_frame_id=frame.frame_id,
                previous_scale=min(0.75, current_scale),
                current_scale=current_scale,
            )
        else:
            state_message = image_message(state_input, png, scale=current_scale)
        messages = [
            cacheable_system_message(
                self._state_system_prompt(),
                enabled=getattr(self, "_worker_explicit_cache", False),
            ),
            state_message,
        ]
        report = _context_size_report(
            "tool_agent.state.context",
            "typed_state_frame_delta",
            state_input,
            mode=mode,
            previous_frame=has_previous_frame,
            previous_frame_scale=(min(0.75, current_scale) if has_previous_frame else None),
            current_frame_scale=current_scale,
            included_count=len(state_payload),
        )
        return messages, [report]

    def _actor_messages(
        self,
        *,
        spec: WorkerSpec,
        journal: WorkerJournal,
        state: WorkerStateSnapshot,
        frame: MaterializedFrame,
        png: bytes,
        same_frame_feedback: dict[str, Any] | None,
    ) -> tuple[list[Any], list[dict[str, Any]]]:
        """Build the action-only Actor call without exposing Runtime history directly."""

        parts = [
            worker_attempt_contract(
                spec,
                attempted_action=bool(journal.executed_tools),
                current_element=self._current_each_element(spec, journal.worker_id),
            ),
        ]
        parts.extend([
            "## Authoritative materialized State view",
            json.dumps(state_actor_payload(state), ensure_ascii=False),
        ])
        receipt = latest_runtime_receipt(journal)
        if receipt is not None:
            parts.extend([
                "## Latest Runtime action receipt",
                json.dumps(receipt, ensure_ascii=False),
            ])
        if same_frame_feedback:
            parts.extend([
                "## Runtime correction for this same frame",
                json.dumps(same_frame_feedback, ensure_ascii=False),
            ])
        actor_input = "\n\n".join(parts)
        messages = [
            cacheable_system_message(
                self._actor_system_prompt(),
                enabled=getattr(self, "_worker_explicit_cache", False),
            ),
            image_message(
                actor_input,
                png,
                scale=float(
                    getattr(getattr(self, "worker_cfg", None), "image_scale", 1.0)
                ),
            ),
        ]
        report = _context_size_report(
            "tool_agent.actor.context",
            "state_actor_split",
            actor_input,
            included_count=(
                3 + int(receipt is not None) + int(bool(same_frame_feedback))
            ),
        )
        return messages, [report]

    def _invoke_state_role(
        self,
        *,
        step: int,
        spec: WorkerSpec,
        journal: WorkerJournal,
        frame: MaterializedFrame,
        messages: list[Any],
        context_reports: list[dict[str, Any]],
    ) -> WorkerStateSnapshot:
        """Run and validate one fact-only State stage before Actor policy."""

        request_kwargs = chat_request_kwargs(
            getattr(getattr(self, "worker_cfg", None), "model", None)
        )
        bound_state = (
            self.worker.bind(
                response_format={"type": "json_object"},
                max_tokens=700,
                **request_kwargs,
            )
            if callable(getattr(self.worker, "bind", None))
            else self.worker
        )
        active_messages = list(messages)
        llm_elapsed_s = 0.0
        response = None
        token_usage: dict[str, int] = {}
        for attempt in range(2):
            started_at = time.perf_counter()
            for transport_attempt in range(2):
                try:
                    response = bound_state.invoke(active_messages)
                    break
                except Exception as exc:  # noqa: BLE001 - provider-neutral retry
                    if transport_attempt or not _is_transient_model_error(exc):
                        raise
                    self._trace(
                        "worker_state_model_retry",
                        step=step,
                        frame_id=frame.frame_id,
                        error=f"{type(exc).__name__}: {exc}",
                    )
            llm_elapsed_s += time.perf_counter() - started_at
            token_usage = response_usage(response)
            try:
                batch = WorkerStateTraceBatch.model_validate(
                    normalize_state_trace_payload(
                        parse_json_object(getattr(response, "content", ""))
                    )
                )
                if batch.frame_id != frame.frame_id:
                    raise ValueError(
                        f"expected frame_id {frame.frame_id!r}, got {batch.frame_id!r}"
                    )
                snapshots = getattr(self, "_worker_state_snapshots", {})
                previous = snapshots.get(journal.worker_id)
                state = reduce_worker_state(
                    previous if isinstance(previous, WorkerStateSnapshot) else None,
                    batch,
                    spec=spec,
                )
                break
            except Exception as exc:  # noqa: BLE001 - one same-frame JSON repair
                self._trace(
                    "worker_state_protocol_error",
                    step=step,
                    frame_id=frame.frame_id,
                    attempt=attempt + 1,
                    error=str(exc),
                    llm_elapsed_s=round(llm_elapsed_s, 3),
                    token_usage=token_usage,
                    context_reports=diagnostic_prompt_reports(
                        "tool_agent.state.protocol_repair",
                        active_messages,
                        response,
                        schema="WorkerStateTraceBatch JSON",
                    ) + context_reports,
                )
                if attempt:
                    raise ProtocolError(
                        f"State repeated an invalid JSON protocol: {exc}"
                    ) from exc
                active_messages = [
                    *active_messages,
                    response,
                    HumanMessage(content=(
                        "State protocol repair on this SAME frame. No action was executed. "
                        f"The prior response was invalid: {exc}. Return only one JSON object "
                        "matching this compact output contract:\n"
                        + json.dumps(
                            STATE_TRACE_OUTPUT_CONTRACT,
                            ensure_ascii=False,
                        )
                    )),
                ]
        assert response is not None
        self._trace(
            "worker_state",
            step=step,
            frame_id=frame.frame_id,
            mode=batch.mode,
            trace_events=[item.model_dump(mode="json") for item in batch.events],
            state=state.model_dump(mode="json"),
            llm_elapsed_s=round(llm_elapsed_s, 3),
            token_usage=token_usage,
            context_reports=[
                *context_reports,
                *diagnostic_prompt_reports(
                    "tool_agent.state",
                    active_messages,
                    response,
                    parsed=state.model_dump(mode="json"),
                    schema="WorkerStateTraceBatch JSON",
                ),
            ],
        )
        return state

    def _worker_tools_for_frame(
        self,
        spec: WorkerSpec,
        actions: list[DynamicActionSpec],
        frame: MaterializedFrame,
        *,
        assessment: WorkerFrameTools | None = None,
        allow_failure: bool = True,
    ) -> list[dict[str, Any]]:
        assessment = assessment or worker_frame_tools(
            spec, actions, frame,
        )
        return dynamic_actor_tools(
            assessment.allowed_actions,
            completion_mode=assessment.completion_mode,
            action_envelope=bool(getattr(self, "allow_multi_action", False)),
            max_ordered_actions=self._worker_action_limit(),
            allow_failure=allow_failure,
        )

    def _worker_action_limit(self) -> int:
        return (
            MAX_ORDERED_ACTIONS
            if bool(getattr(self, "allow_multi_action", False))
            else 1
        )

    def _visible_commit_controls(
        self,
        frame: MaterializedFrame,
        state: WorkerStateSnapshot,
    ) -> list[dict[str, str]]:
        """Expose enabled commit controls cited by the completion claim itself."""

        controls = frame.controls
        refresh = getattr(getattr(self, "_executor", None), "refresh_controls", None)
        if callable(refresh):
            try:
                refreshed = refresh()
            except Exception:  # noqa: BLE001 - completion audit is best-effort
                refreshed = None
            if isinstance(refreshed, list):
                controls = refreshed
                frame.controls = refreshed
        claim = " ".join(state.fact_statements).casefold()

        def cited(control: dict[str, Any]) -> bool:
            label = str(control.get("label") or "").strip().casefold()
            return bool(label and re.search(
                rf"(?<!\w){re.escape(label)}(?!\w)", claim,
            ))

        return [
            {
                "label": str(control.get("label") or ""),
                "kind": str(control.get("kind") or ""),
            }
            for control in controls
            if isinstance(control, dict)
            and control.get("form_action") == "commit"
            and control.get("in_viewport") is not False
            and control.get("enabled") is not False
            and cited(control)
        ]

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
        if control is None or control.get("enabled") is False:
            return False
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
        *,
        max_actions: int = MAX_ORDERED_ACTIONS,
    ) -> None:
        if not 1 <= len(calls) <= max_actions:
            raise ProtocolError(f"action envelope must contain 1–{max_actions} actions")
        action_names = {action.name for action in actions}
        unknown = [call["name"] for call in calls if call["name"] not in action_names]
        if unknown:
            raise ProtocolError(
                "multi-action output may contain only executable action tools; got "
                + ", ".join(unknown)
            )
        action_by_name = {action.name: action for action in actions}
        for index, call in enumerate(calls):
            ToolAgentRuntime._validated_action_call_args(
                action_by_name[call["name"]],
                call["args"],
            )
            capability = action_by_name[call["name"]].capability
            if index < len(calls) - 1 and capability in _BATCH_FINAL_CAPABILITIES:
                raise ProtocolError(
                    f"{capability} changes geometry or surface and must be the final batch action; "
                    "launch_app can run directly without home or app_switch"
                )

    @staticmethod
    def _validated_action_call_args(
        action: DynamicActionSpec,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        """Normalize provider quirks and validate before platform dispatch."""

        parameters = dynamic_action_tool(
            action,
            include_state_target_ref="state_target_ref" in args,
        )["function"]["parameters"]
        call_args = dict(args)
        for name in action.fixed_args:
            # Fixed Runtime-owned values are authoritative. Some providers echo
            # them despite the reduced tool schema.
            call_args.pop(name, None)
        if "description" in parameters.get("properties", {}) and not str(
            call_args.get("description") or ""
        ).strip():
            call_args["description"] = action.description
        validate(instance=call_args, schema=parameters)
        return call_args

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

    def _execute_multi_action_calls(
        self,
        *,
        worker_id: str,
        spec: WorkerSpec,
        actions: list[DynamicActionSpec],
        calls: list[dict[str, Any]],
        state: WorkerStateSnapshot,
        step: int,
        frame: MaterializedFrame,
        png: bytes,
        journal: WorkerJournal,
        commitment_refs: tuple[str, ...] = (),
        observed_auth_codes: set[str] | None = None,
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
        memory_commit_safe = True
        reason = ""
        terminal: str | None = None
        rejected_before_dispatch = False
        for index, action_call in enumerate(calls, start=1):
            action_spec = action_by_name[action_call["name"]]
            if action_call.get("_control_ref") and not self._refresh_next_action(
                action_call, action_spec, frame,
            ):
                reason = "current controls could not rebind the action suffix"
                break
            resolved_args = {
                "description": action_spec.description,
                **action_spec.fixed_args,
                **action_call["args"],
            }
            remaining = calls[index:]
            boundary_error = (
                _state_target_binding_error(
                    state,
                    action_spec.capability,
                    action_call.get("_state_target_ref", ""),
                )
                if isinstance(state, WorkerStateSnapshot)
                else ""
            )
            if not boundary_error:
                boundary_error = action_boundary_error(
                    action_spec.capability,
                    resolved_args,
                    frame,
                    observed_auth_codes or set(),
                )
            if boundary_error:
                reason = boundary_error
                rejected_before_dispatch = executed == 0
                journal.record_guard(
                    step=step,
                    repair_turn=index,
                    tool=action_call["name"],
                    reason=reason,
                )
                break
            commitment_statement = str(
                action_call["args"].get("description") or action_spec.description
            )
            dispatched_commitment = journal.record_runtime_commitment(
                step=step, substep=index, frame_id=frame.frame_id,
                tool=action_call["name"], statement=commitment_statement,
                status="dispatched",
            )
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
                    worker_id=worker_id,
                )
            except _WorkerActionRejected as exc:
                result = _worker_action_error(exc)
                terminal = None
                rejected_before_dispatch = True
            except Exception as exc:  # noqa: BLE001 - abort suffix and reobserve
                result = _worker_action_error(exc)
                terminal = None
            if not rejected_before_dispatch:
                executed += 1
            journal.record_action_result(
                step=step,
                substep=index,
                frame_id=frame.frame_id,
                tool=action_call["name"],
                args=action_call["args"],
                result=result,
                commitment_refs=(*commitment_refs, dispatched_commitment.event_ref),
                surface_fingerprint=frame.visual_fingerprint,
            )
            journal.settle_runtime_commitment(
                step=step, substep=index, frame_id=frame.frame_id,
                tool=action_call["name"], statement=commitment_statement,
                result=result,
            )
            journal.record_runtime_result(
                step=step,
                substep=index,
                result=result,
            )
            if (
                result.get("status") != "executed"
                or result.get("no_effect") is True
                or (result.get("target_signal") or {}).get("status") == "off_target"
            ):
                memory_commit_safe = False
            if rejected_before_dispatch:
                reason = str(result["error"])
                break
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
            frame.visual_fingerprint = visual_surface_fingerprint(current_png)
            frame.url = next_page_identity[0] or frame.url
            frame.title = next_page_identity[1] or frame.title

        payload = {
            "status": "executed" if executed == len(calls) and not reason else "aborted",
            "planned_actions": len(calls),
            "executed_actions": executed,
            "_memory_commit_safe": (
                memory_commit_safe and executed == len(calls) and not reason
            ),
        }
        event = (
            "worker_multi_action_completed"
            if payload["status"] == "executed"
            else "worker_multi_action_aborted"
        )
        if reason:
            payload["reason"] = reason
        if executed == 0 and rejected_before_dispatch:
            payload["reuse_current_frame"] = True
        self._trace(event, worker_id=worker_id, step=step, **payload)
        return payload, terminal

    def _execute_worker_tool(
        self,
        spec: WorkerSpec,
        actions: list[DynamicActionSpec],
        call: dict[str, Any],
        png: bytes,
        frame: MaterializedFrame | None = None,
        *,
        worker_id: str = "",
    ) -> tuple[dict[str, Any], str | None]:
        if call["name"] == "complete":
            parsed_complete = CompleteReadyWorkerArgs.model_validate(call["args"])
            if spec.profile == "collector":
                if frame is None:
                    raise ValueError("collector complete requires a current frame")
                # The Worker may fold records read from un-extractable surfaces into
                # the completion; accumulate them before binding the collection.
                if parsed_complete.rows:
                    self._accumulate_observed_rows(
                        spec, worker_id, {"rows": parsed_complete.rows}, 0,
                    )
                # The Worker certifies exhaustiveness from its own evidence; Runtime
                # binds whatever rows the perception loop has accumulated so far.
                requirement = spec.data_requirements[0]
                descriptor = self.data_store.collection_for_requirement(requirement.id)
                if descriptor is None:
                    raise ValueError(
                        f"collector complete has no accumulated rows for requirement "
                        f"{requirement.id!r}; keep collecting or report_blocked"
                    )
                return descriptor.model_dump(mode="json"), "complete"
            # Operator with consume="each" (or an array ref) bindings: `complete`
            # after finishing one plan element advances the shared cursor and
            # continues the Worker unless the array is exhausted (then it is a
            # real terminal). Uses the same predicate as materialization so an
            # array ref bound without explicit consume still iterates.
            each_refs = sorted({
                binding.input
                for binding in spec.input_bindings
                if self._binding_is_each(spec, binding)
            })
            if each_refs:
                for ref_name in each_refs:
                    values = self.data_store.result_value(spec.input_refs[ref_name])
                    cursor = self._each_cursors.get((worker_id, ref_name), 0)
                    if cursor < len(values):
                        self._each_cursors[(worker_id, ref_name)] = cursor + 1
                remaining = any(
                    self._each_cursors.get((worker_id, ref_name), 0)
                    < len(self.data_store.result_value(spec.input_refs[ref_name]))
                    for ref_name in each_refs
                )
                if remaining:
                    return {"status": "completed", "each_advanced": True}, "each_next"
            return {"status": "completed"}, "complete"
        if call["name"] == "report_blocked":
            parsed = FailWorkerArgs.model_validate(call["args"])
            return {"status": "failed", "reason": parsed.reason}, "report_blocked"
        action_by_name = {item.name: item for item in actions}
        action_spec = action_by_name.get(call["name"])
        if action_spec is None:
            raise ProtocolError(f"unknown Worker tool {call['name']!r}")
        call_args = self._validated_action_call_args(action_spec, call["args"])
        full_args = {**action_spec.fixed_args, **call_args}
        if action_spec.capability == "open_url":
            self._validate_runtime_open_url(str(full_args.get("url") or ""))
        if action_spec.capability == "launch_app":
            self._validate_runtime_launch_app(str(full_args.get("app") or ""))
        if action_spec.capability == "ask_user":
            request_user_input = getattr(self.bundle, "request_user_input", None)
            if not callable(request_user_input):
                raise _WorkerActionRejected(
                    "ask_user is unavailable because the platform has no user-input bridge"
                )
            question = str(full_args.get("question") or "").strip()
            answer = str(request_user_input(question) or "").strip()
            if not answer:
                raise RuntimeError("ask_user returned an empty response")
            payload = {
                "status": "executed",
                "action_type": "ask_user",
                "no_effect": False,
                "_runtime_memory_statement": (
                    f"Authoritative user response to {question!r}: {answer}"
                ),
            }
            self._trace(
                "runtime_action",
                tool=call["name"],
                profile=spec.profile,
                status="executed",
                action_type="ask_user",
                no_effect=False,
            )
            return payload, None
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
            target_signal: dict[str, Any] | None = None
            verify_future = None
            verify_pool = getattr(self, "_target_verify_pool", None)
            ground_pool = getattr(self, "_target_ground_pool", None)
            can_verify_target = bool(
                verify_pool is not None
                and executed_action.action_type in _TARGET_VERIFIED_ACTION_TYPES
                and executed_action.x is not None
                and executed_action.y is not None
            )
            can_ground_target = bool(
                ground_pool is not None
                and executed_action.action_type in _TARGET_VERIFIED_ACTION_TYPES
                and executed_action.x is not None
                and executed_action.y is not None
                and not has_snapped_point(decision)
            )
            grounding: TargetGrounding | None = None
            if can_ground_target:
                ground_future = ground_pool.submit(
                    ground_target,
                    png,
                    float(executed_action.x),
                    float(executed_action.y),
                    str(executed_action.description or ""),
                    str(executed_action.action_type or ""),
                )
                grounding, ground_error = _vision_future_result(
                    ground_future, TargetGrounding,
                )
                if ground_error is not None:
                    self._trace(
                        "worker_target_grounding_error",
                        tool=call["name"],
                        error=f"{type(ground_error).__name__}: {ground_error}",
                    )
                elif grounding is not None:
                    decision, target_signal, proposed_inside, rejection = (
                        resolve_target_grounding(decision, grounding)
                    )
                    self._trace(
                        "worker_target_grounding",
                        tool=call["name"],
                        target_found=grounding.target_found,
                        target_box=(
                            list(grounding.target_box)
                            if grounding.target_box is not None else None
                        ),
                        control_type=grounding.control_type,
                        label=grounding.label,
                        container_context=grounding.container_context,
                        confidence=grounding.confidence,
                        proposed_inside=proposed_inside,
                        reason=grounding.reason,
                    )
                    if rejection:
                        raise _WorkerActionRejected(rejection)
                    executed_action = decision.action
            # Typing is a composite, destructive action: it taps, clears, then
            # enters text. Reject a visually disproven field before dispatch so
            # an off-target point cannot clear or type into an adjacent control.
            if (
                can_verify_target
                and executed_action.action_type == "type"
                and target_signal is None
            ):
                verify_future = verify_pool.submit(
                    verify_target,
                    png,
                    float(executed_action.x),
                    float(executed_action.y),
                    str(executed_action.description or ""),
                )
                target_signal, verify_error = _target_verification_result(verify_future)
                verify_future = None
                if verify_error is not None:
                    self._trace(
                        "worker_target_verify_error",
                        tool=call["name"],
                        error=f"{type(verify_error).__name__}: {verify_error}",
                    )
                elif target_signal and target_signal.get("status") == "off_target":
                    actual = str(target_signal.get("actual_element") or "").strip()
                    reason = str(target_signal.get("reason") or "").strip()
                    detail = f" on {actual!r}" if actual else ""
                    suffix = f": {reason}" if reason else ""
                    raise _WorkerActionRejected(
                        f"predispatch target verifier reported off_target{detail}{suffix}"
                    )
            # Grounding has already produced the best currently known point.
            # Show it before dispatch, then mirror the original runtime contract
            # by updating once more if the executor records a DOM snap.
            self._show_action(decision.action)
            executed = self._executor.execute(decision, png_bytes=png)
            if executed and has_snapped_point(decision):
                self._show_action(decision.action)
            if (
                executed
                and can_verify_target
                and executed_action.action_type != "type"
                and target_signal is None
            ):
                verify_future = verify_pool.submit(
                    verify_target,
                    png,
                    float(executed_action.x),
                    float(executed_action.y),
                    str(executed_action.description or ""),
                )
            try:
                if executed:
                    focus_y = (
                        float(executed_action.y)
                        if executed_action.action_type == "type"
                        and executed_action.y is not None
                        else None
                    )
                    center = (
                        (float(executed_action.x), float(executed_action.y))
                        if executed_action.action_type
                        in {"tap", "click", "long_press", "select_option"}
                        and executed_action.x is not None
                        and executed_action.y is not None
                        else None
                    )
                    elapsed, no_effect = settle_after_action(
                        self.platform,
                        png,
                        action_type=executed_action.action_type,
                        focus_y=focus_y,
                        center=center,
                    )
                else:
                    elapsed, no_effect = 0.0, True
            except BaseException:
                if verify_future is not None:
                    verify_future.cancel()
                raise
            if verify_future is not None:
                target_signal, verify_error = _target_verification_result(verify_future)
                if verify_error is not None:
                    self._trace(
                        "worker_target_verify_error",
                        tool=call["name"],
                        error=f"{type(verify_error).__name__}: {verify_error}",
                    )
            feedback_reader = getattr(
                getattr(self.platform, "client", None),
                "consume_action_feedback",
                None,
            )
            platform_feedback = _action_feedback(
                feedback_reader() if callable(feedback_reader) else [],
                executed_action.action_type,
            )
            rejected_feedback = any(
                item.get("rejected") is True for item in platform_feedback
            )
            terminal = (
                "navigation_blocked"
                if rejected_feedback and executed_action.action_type == "navigate"
                else "platform_rejected" if rejected_feedback else None
            )
            payload = {
                "status": "executed" if executed and not rejected_feedback else "failed",
                "action_type": executed_action.action_type,
                "settle_seconds": round(elapsed, 3),
                "no_effect": no_effect,
                "grounding": getattr(decision.action, "snap", None),
            }
            if executed_action.action_type == "scroll":
                payload["direction"] = executed_action.direction
            if target_signal is not None:
                payload["target_signal"] = target_signal
            if platform_feedback:
                payload["platform_feedback"] = platform_feedback
            if terminal is not None:
                rejection = next(
                    (
                        item for item in platform_feedback
                        if item.get("rejected") is True
                    ),
                    {},
                )
                payload["reason"] = str(
                    rejection.get("message")
                    or "The platform rejected the requested action."
                )
            if frame is not None and payload["status"] == "executed" and (
                payload["no_effect"] is False
                and is_confirmed_selection_commit(
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
    ) -> None:
        """Validate only URL shape and network safety before dispatch."""

        admission = assess_navigation_url(candidate)
        if admission.decision != "allow":
            raise _WorkerActionRejected(admission.reason)

    @staticmethod
    def _validate_worker_spec(spec: WorkerSpec) -> None:
        for requirement in spec.data_requirements:
            Draft202012Validator.check_schema(requirement.row_schema)
        for binding in spec.input_bindings:
            ToolAgentRuntime._validate_action_spec(input_binding_action(binding))

    @staticmethod
    def _validate_action_spec(action: DynamicActionSpec) -> None:
        validate_dynamic_action_spec(action)

    def _platform_name(self) -> str:
        bundle = getattr(self, "bundle", None)
        return str(getattr(bundle, "platform", "browser") or "browser")

    def _platform_prompt_context(self) -> dict[str, Any]:
        return {
            "name": self._platform_name(),
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
        raise _WorkerActionRejected(
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
        worker_id = str(getattr(self, "_active_worker_id", "") or "")
        binding_by_name = {item.name: item for item in spec.input_bindings}
        resolved = dict(action.fixed_args)
        for argument, binding in action.input_args.items():
            try:
                value = self.data_store.result_value(spec.input_refs[binding.input])
                declared = binding_by_name.get(action.name)
                if declared is not None and declared.consume == "each" or (
                    isinstance(value, list) and declared is not None
                ):
                    cursor = self._each_cursors.get((worker_id, binding.input), 0)
                    if cursor >= len(value):
                        raise _EachExhausted(
                            f"{action.name}: each array for {binding.input!r} exhausted "
                            f"at cursor {cursor}/{len(value)}"
                        )
                    value = value[cursor]
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
        actions = [
            generic_action_spec(capability)
            for capability in sorted(self._supported_capabilities())
        ]
        for binding in spec.input_bindings:
            try:
                actions.append(self._materialize_action_inputs(
                    spec, input_binding_action(binding),
                ))
            except _EachExhausted:
                # The array is already fully consumed; the binding action is not
                # available on this Worker run.
                continue
            except ValueError:
                # Non-each binding that fails materialization is surfaced at
                # dispatch; do not silently drop it here.
                if self._binding_is_each(spec, binding):
                    raise
                continue
        for action in actions:
            self._validate_platform_action(action)
        return actions

    def _binding_is_each(self, spec: WorkerSpec, binding: Any) -> bool:
        """Whether a binding consumes its ref element-wise.

        Either the Master declared consume="each", or the ref resolves to a list
        (an array plan routed to a Worker is inherently element-wise)."""

        if getattr(binding, "consume", "once") == "each":
            return True
        try:
            return isinstance(
                self.data_store.result_value(spec.input_refs[binding.input]), list
            )
        except (KeyError, TypeError):
            return False

    def _accumulate_observed_rows(
        self,
        spec: WorkerSpec,
        worker_id: str,
        call: dict[str, Any],
        step: int,
    ) -> None:
        """Fold Worker-observed structured rows into the collection.

        General fallback for surfaces perception cannot extract (detail pages,
        dialogs, non-standard grids). The Worker declares the exact records it
        read from the current view; Runtime validates and accumulates them so a
        transform can consume them even when perception produced no rows.
        """

        rows = call.get("rows")
        if not rows or spec.profile != "collector" or not spec.data_requirements:
            return
        requirement = spec.data_requirements[0]
        if not isinstance(requirement.row_schema, dict):
            return
        try:
            self.data_store.put_observed_rows(
                requirement.id, rows, requirement.row_schema,
            )
        except ValueError as exc:
            self._trace(
                "worker_observed_rows_rejected",
                worker_id=worker_id,
                requirement_id=requirement.id,
                reason=str(exc),
                step=step,
            )
            return
        self._trace(
            "worker_observed_rows",
            worker_id=worker_id,
            requirement_id=requirement.id,
            count=len(rows),
            step=step,
        )

    def _current_each_element(self, spec: WorkerSpec, worker_id: str) -> str | None:
        """The current consume="each" plan element as a worker-visible locating hint.

        The bound values are private-value actions the Worker never sees; without
        this hint a Worker iterating an array plan cannot know which record to
        locate on screen and falls back to the first visible row. Emit one line
        per each-binding naming the field the binding's path resolves to.
        """

        each = [
            binding for binding in spec.input_bindings
            if self._binding_is_each(spec, binding)
        ]
        if not each:
            return None
        lines: list[str] = []
        for binding in each:
            try:
                values = self.data_store.result_value(spec.input_refs[binding.input])
            except (KeyError, TypeError):
                continue
            cursor = self._each_cursors.get((worker_id, binding.input), 0)
            if not isinstance(values, list) or cursor >= len(values):
                continue
            element = values[cursor]
            try:
                for part in binding.path:
                    element = element[part]
            except (KeyError, TypeError, IndexError):
                element = None
            if element is not None:
                lines.append(f"{binding.name}={element}")
        return "; ".join(lines) if lines else None

    def _refresh_each_actions(
        self,
        spec: WorkerSpec,
        actions: list[DynamicActionSpec],
    ) -> list[DynamicActionSpec]:
        """Re-materialize consume="each" binding actions at the current cursor.

        The cursor advances when the Worker calls `complete` after finishing one
        plan element; each new frame must see the next element's bound value.
        Each binding is rebuilt from its original semantic binding (an already
        materialized action has empty input_args and would not refresh).
        Non-each actions pass through unchanged.
        """

        each_bindings = [
            binding for binding in spec.input_bindings
            if self._binding_is_each(spec, binding)
        ]
        if not each_bindings:
            return actions
        each_by_name = {binding.name: binding for binding in each_bindings}
        refreshed: list[DynamicActionSpec] = []
        for action in actions:
            binding = each_by_name.get(action.name)
            if binding is None:
                refreshed.append(action)
                continue
            try:
                refreshed.append(self._materialize_action_inputs(
                    spec, input_binding_action(binding),
                ))
            except _EachExhausted:
                # Cursor passed the end of the plan array: the binding action is
                # no longer available; the Worker has finished all plan elements.
                continue
        return refreshed

    @staticmethod
    def _event_layer(event: str) -> str:
        if event.startswith(("master_", "strategy_")):
            return event.split("_", 1)[0]
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
        if event == "worker_state":
            state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
            return (
                f"State step {payload.get('step', '?')} "
                f"[{state.get('status', '?')}]: {state.get('summary', '')}"
            )
        if event == "worker_decision":
            state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
            summary = str(state.get("summary") or "").strip()
            return (
                f"Worker step {payload.get('step', '?')} [{state.get('status', '?')}] "
                f"→ {payload.get('tool', '?')}"
                + (f": {summary}" if summary else "")
            )
        if event == "worker_target_grounding":
            target = str(payload.get("label") or payload.get("control_type") or "target")
            confidence = str(payload.get("confidence") or "unknown")
            relation = "inside" if payload.get("proposed_inside") else "outside"
            return f"Visual grounding {target!r}: {confidence}, point {relation} target box"
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
        if event == "master_worker_result":
            outcome = payload.get("outcome") if isinstance(payload.get("outcome"), dict) else {}
            return f"Worker {payload.get('worker_id', '?')} returned {outcome.get('phase', '?')}"
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
                f"Goal    : {entry.get('goal', '')}\n"
                + (f"Success :\n{criteria_text}" if criteria_text else "")
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
            capture_timing = (
                entry.get("capture_timing")
                if isinstance(entry.get("capture_timing"), dict)
                else {}
            )
            timing_text = " · ".join(
                f"{label}={float(value):.1f}s"
                for label, value in {
                    "total": entry.get("observe_seconds"),
                    "hierarchy": capture_timing.get("hierarchy_seconds"),
                    "pixels": capture_timing.get("screenshot_seconds"),
                }.items()
                if value is not None
            )
            control_count = entry.get("control_count")
            control_text = (
                f" · {int(control_count)} controls"
                if isinstance(control_count, int)
                else ""
            )
            return (
                f"\n--- Turn {turn_no} ---\n"
                + (f"Screenshot : {screenshot}\n" if screenshot else "")
                + f"Observation: {entry.get('mode', '?')} perception{control_text}\n"
                + (f"Timing     : {timing_text}\n" if timing_text else "")
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
                    "worker_state": "state",
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
            return (
                f"State      : {state.get('status', '?')} · {state.get('summary', '')}\n"
                f"Action plan: {entry.get('tool', '?')}"
                f"{context_text}"
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
        if event == "worker_target_grounding":
            target = str(entry.get("label") or entry.get("control_type") or "target")
            confidence = str(entry.get("confidence") or "unknown")
            relation = "inside" if entry.get("proposed_inside") else "outside"
            box = entry.get("target_box")
            return (
                f"Grounding  : {confidence} · {target} · point {relation}"
                + (f" · box={box}" if box else "")
            )
        if event == "worker_same_frame_action_repair":
            return (
                "Recovery   : action rejected before dispatch · reuse current frame "
                f"({entry.get('repair_turn', '?')}/"
                f"{_MAX_PREDISPATCH_REPAIRS_PER_FRAME})"
            )
        if event == "worker_action_rejected":
            reason = str(entry.get("reason") or "")
            return f"Action rejected: {entry.get('tool', '?')} · {reason}"
        if event == "worker_complete":
            collection = (
                entry.get("collection_ref")
                if isinstance(entry.get("collection_ref"), dict)
                else {}
            )
            suffix = f" · {collection.get('ref')}" if collection.get("ref") else ""
            return f"Verification: completed{suffix}"
        if event == "master_worker_result":
            outcome = entry.get("outcome") if isinstance(entry.get("outcome"), dict) else {}
            return f"Worker outcome: {entry.get('worker_id', '?')} · {outcome.get('phase', '?')}"
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
            "platform_time": getattr(self, "platform_time", None).model_dump(mode="json")
            if getattr(self, "platform_time", None) is not None
            else {},
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
