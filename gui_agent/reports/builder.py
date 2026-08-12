"""Build report data models from exploration and execution logs."""

from __future__ import annotations

import io
import json
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image

from .images import (
    _load_img,
    _save_report_img,
    annotate_action,
    annotate_back_attempts_img,
    annotate_recon_taps,
)
from .metrics import _MODELS_MAP, _sum_tokens, _token_cost
from .statement_reducer import StatementReportReducer
from .models import (
    AppReconData,
    ReconFlow,
    ReconPageInfo,
    ReconTap,
    ReportData,
    ReportPage,
    ReportStep,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _reduce_tool_agent_events(events: list[dict]) -> list[dict]:
    """Collapse one visual Worker turn into one report timeline entry.

    The JSONL trace remains lossless. The HTML timeline is a narrative projection:
    observe, state/action decisions, same-frame action patches, execution and protocol
    telemetry belong to one Worker turn rather than several unrelated cards.
    """
    projected: list[dict] = []
    pending: dict | None = None
    pre_observe_details: list[dict] = []

    def flush() -> None:
        nonlocal pending
        if pending is None:
            return
        decisions = pending.get("decisions") or []
        final_decision = decisions[-1] if decisions else {}
        state = (
            final_decision.get("state")
            if isinstance(final_decision.get("state"), dict)
            else {}
        )
        action_events = pending.get("action_events") or []
        actions = [item for item in action_events if item.get("event") == "runtime_action"]
        final_action = actions[-1] if actions else {}
        pending.update({
            "event": "worker_turn",
            "state": state,
            "tool": final_decision.get("tool") or final_action.get("tool") or "",
            "args": final_decision.get("args") or {},
            "action": final_action,
            "message": state.get("summary") or pending.get("message") or "",
            "has_error": bool(pending.get("errors")),
        })
        projected.append(pending)
        pending = None

    for event in events:
        name = str(event.get("event") or "")
        if name == "perception_extract":
            # Perception is emitted immediately before its ``observe`` event, so
            # once a prior Turn is pending it already belongs to the next frame.
            pre_observe_details.append(event)
            continue
        if name == "worker_state_recovered":
            if pending is not None:
                pending.setdefault("details", []).append(event)
            else:
                pre_observe_details.append(event)
            continue
        if name == "observe":
            flush()
            pending = {
                **event,
                "observation": event,
                "decisions": [],
                "action_events": [],
                "errors": [],
                "details": pre_observe_details,
            }
            pre_observe_details = []
            continue
        if pending is not None and name == "worker_decision":
            pending["decisions"].append(event)
            pending["step"] = event.get("step")
            pending["profile"] = event.get("profile") or pending.get("profile")
            pending["worker_id"] = event.get("worker_id") or pending.get("worker_id")
            continue
        if pending is not None and name in {"runtime_action", "runtime_action_started"}:
            pending["action_events"].append(event)
            continue
        if pending is not None and name in {
            "worker_action_patch",
            "worker_action_patch_error",
            "worker_action_blocked",
            "worker_protocol_error",
            "worker_tool_error",
            "worker_complete",
            "worker_multi_action_completed",
            "worker_multi_action_aborted",
        }:
            pending["details"].append(event)
            if name in {
                "worker_action_patch_error",
                "worker_tool_error",
            }:
                pending["errors"].append(event)
            continue
        flush()
        projected.append(event)
    flush()
    return projected


def _tool_agent_report_steps(
    run_dir: Path,
    orchestrator: dict,
    goal: str = "",
) -> tuple[list[ReportPage], list[dict], dict]:
    """Project a Tool Agent run into Master, GUI Worker and transform layers.

    The persisted JSONL remains event-oriented.  The report is task-oriented: the
    Master program is rendered as the plan, each autonomous GUI Worker is
    one statement card, and one visual frame becomes one Turn.  Runtime diagnostics
    stay nested inside their owning Turn instead of becoming timeline cards.
    """
    trace_path = run_dir / "tool_agent_trace.json"
    if not trace_path.is_file():
        configured = Path(str(orchestrator.get("trace_path") or ""))
        if configured.is_file():
            trace_path = configured
        else:
            events_path = run_dir / "tool_agent_events.jsonl"
            if not events_path.is_file():
                return [], [], {"turns": 0, "executed": 0}
            events = []
            for line in events_path.read_text(encoding="utf-8").splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    events.append(event)
            raw = {"phase": "running", "trace": events}
    if trace_path.is_file():
        raw = json.loads(trace_path.read_text(encoding="utf-8"))
    replay_path = run_dir / "tool_agent_replay.json"
    if replay_path.is_file():
        try:
            replay = json.loads(replay_path.read_text(encoding="utf-8"))
            if isinstance(replay, dict):
                orchestrator["replay"] = replay
        except (OSError, json.JSONDecodeError):
            pass
    presentation_path = run_dir / "tool_agent_presentation.json"
    if presentation_path.is_file():
        try:
            presentation = json.loads(
                presentation_path.read_text(encoding="utf-8")
            )
            if isinstance(presentation, dict):
                orchestrator["presentation"] = presentation
        except (OSError, json.JSONDecodeError):
            pass
    events = [item for item in (raw.get("trace") or []) if isinstance(item, dict)]
    timeline_events = _reduce_tool_agent_events(events)
    screenshots = sorted(
        run_dir.glob("screenshot_tool_agent_*.png"),
        key=lambda path: int(path.stem.rsplit("_", 1)[-1]),
    )
    first_screenshot = screenshots[0].name if screenshots else ""
    frame_screenshots = {
        f"frame:{index}": path.name for index, path in enumerate(screenshots, 1)
    }
    runtime_action_types = {
        str(event.get("tool") or ""): str(event.get("action_type") or "")
        for event in events if event.get("event") == "runtime_action"
    }

    def event_timestamp(event: dict) -> str:
        timestamp = str(event.get("timestamp") or "")
        if timestamp:
            return timestamp
        try:
            started = datetime.strptime(run_dir.name, "%Y%m%d_%H%M%S")
            return (started + timedelta(seconds=float(event.get("elapsed_s") or 0))).isoformat()
        except (TypeError, ValueError):
            return ""

    def turn_metrics(turn: dict) -> tuple[dict[str, float], dict[str, dict[str, int]], int]:
        timings: dict[str, float] = {}
        tokens: dict[str, dict[str, int]] = {}
        calls = 0
        metric_events = [
            *(turn.get("details") or []),
            *(turn.get("decisions") or []),
        ]
        for item in metric_events:
            if not isinstance(item, dict):
                continue
            name = str(item.get("event") or "")
            module = (
                "tool_agent.perception"
                if name == "perception_extract"
                else "tool_agent.worker"
            )
            elapsed = float(item.get("llm_elapsed_s") or 0)
            usage = item.get("token_usage") if isinstance(item.get("token_usage"), dict) else {}
            if elapsed > 0:
                timings[module] = timings.get(module, 0.0) + elapsed
            if usage:
                calls += 1
                target = tokens.setdefault(
                    module,
                    {"input": 0, "output": 0, "cached_input": 0},
                )
                target["input"] += int(usage.get("input") or 0)
                target["output"] += int(usage.get("output") or 0)
                target["cached_input"] += int(usage.get("cached_input") or 0)
        return timings, tokens, calls

    def annotated_frame(screenshot: str, annotations: list[dict]) -> tuple[str, str]:
        source_path = run_dir / screenshot
        if not source_path.is_file():
            return screenshot, screenshot
        ann_name = f"{source_path.stem}_ann.jpg"
        full_name = f"{source_path.stem}_ann_full.jpg"
        ann_path = run_dir / ann_name
        full_path = run_dir / full_name
        try:
            image = _load_img(source_path)
            annotated = False
            for item in annotations:
                item_args = item.get("args") if isinstance(item.get("args"), dict) else {}
                x = item_args.get("x")
                y = item_args.get("y")
                if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
                    continue
                image = annotate_action(
                    image,
                    str(item.get("action_type") or "command"),
                    float(x),
                    float(y),
                    int(item.get("index") or 1),
                    direction=str(item_args.get("direction") or "") or None,
                    text=str(
                        item_args.get("text")
                        or item_args.get("description")
                        or ""
                    ) or None,
                )
                annotated = True
            if not annotated:
                return screenshot, screenshot
            _save_report_img(image, ann_path)
            _save_report_img(image, full_path, max_w=None)
            return ann_name, full_name
        except Exception:
            return screenshot, screenshot

    def compact_observation(observation: dict) -> dict:
        collections = []
        for item in observation.get("collections") or []:
            if not isinstance(item, dict):
                continue
            coverage = item.get("coverage") if isinstance(item.get("coverage"), dict) else {}
            collections.append({
                "ref": item.get("ref"),
                "requirement": item.get("requirement_id"),
                "rows": item.get("row_count"),
                "status": coverage.get("status"),
                "known_total": coverage.get("known_total"),
                "scope_status": coverage.get("scope_status"),
                "source_scope": coverage.get("source_scope"),
            })
        return {
            "frame": observation.get("frame_id"),
            "mode": observation.get("mode"),
            "scope": observation.get("requirement_scopes") or {},
            "chunks": [
                {
                    "ref": item.get("ref"),
                    "provider": item.get("provider"),
                    "rows": item.get("row_count"),
                }
                for item in (observation.get("chunks") or [])
                if isinstance(item, dict)
            ],
            "collections": collections,
            "controls": observation.get("control_count", 0),
        }

    def compact_action_batch(turn: dict, decision: dict) -> dict | None:
        decision_args = decision.get("args") if isinstance(decision.get("args"), dict) else {}
        calls = [item for item in (decision_args.get("actions") or []) if isinstance(item, dict)]
        if not calls:  # Compatibility with early multi-action experiment traces.
            calls = [
                {"name": name, "args": args}
                for name, args in zip(
                    decision.get("ordered_tools") or [],
                    decision.get("ordered_args") or [],
                )
            ]
        action_events = turn.get("action_events") or []
        results = [item for item in action_events if item.get("event") == "runtime_action"]
        outcome = next((
            item for item in reversed(turn.get("details") or [])
            if str(item.get("event") or "").startswith("worker_multi_action_")
        ), {})
        planned = int(outcome.get("planned_actions") or len(calls))
        executed_count = int(outcome.get("executed_actions") or len(results))
        is_aborted = str(outcome.get("event") or "").endswith("aborted")
        if planned <= 1 and not is_aborted:
            return None

        items = []
        for offset in range(planned):
            result = results[offset] if offset < len(results) else {}
            call = calls[offset] if offset < len(calls) else {}
            tool = str(call.get("name") or result.get("tool") or "action")
            args = call.get("args") if isinstance(call.get("args"), dict) else {}
            action_type = str(
                result.get("action_type")
                or runtime_action_types.get(tool)
                or "command"
            )
            items.append({
                "index": offset + 1,
                "tool": tool,
                "action_type": action_type,
                "description": str(args.get("description") or ""),
                "args": args,
                "status": (
                    str(result.get("status") or "executed")
                    if result else "discarded" if is_aborted else "planned"
                ),
                "no_effect": bool(result.get("no_effect")),
                "settle_seconds": float(result.get("settle_seconds") or 0),
            })
        return {
            "status": "aborted" if is_aborted else "completed",
            "planned": planned,
            "executed": executed_count,
            "reason": str(outcome.get("reason") or ""),
            "actions": items,
        }

    compile_attempts = [
        event for event in events if event.get("event") == "master_compile_attempt"
    ]
    program_event = next(
        (event for event in reversed(events) if event.get("event") == "master_program_generated"),
        None,
    )
    if program_event is not None:
        master_usage = {"input": 0, "output": 0, "cached_input": 0}
        master_elapsed = 0.0
        for attempt in compile_attempts:
            usage = attempt.get("token_usage") if isinstance(attempt.get("token_usage"), dict) else {}
            master_usage["input"] += int(usage.get("input") or 0)
            master_usage["output"] += int(usage.get("output") or 0)
            master_usage["cached_input"] += int(usage.get("cached_input") or 0)
            master_elapsed += float(attempt.get("llm_elapsed_s") or 0)
        orchestrator["program"] = {
            "kind": "coding",
            "label": "Coding Master · Python orchestration",
            "downstream_label": "Agentic Worker cards (GUI + data)",
            "goal": goal,
            "source": str(program_event.get("source") or ""),
        }
        orchestrator["llm_calls"] = len(compile_attempts)
        orchestrator["timings"] = {"tool_agent.master": master_elapsed}
        orchestrator["token_usage"] = {"tool_agent.master": master_usage}
        orchestrator["compile_attempts"] = [
            {
                "generation": item.get("generation"),
                "attempt": item.get("attempt"),
                "passed": not bool(item.get("diagnostics")),
                "diagnostics": item.get("diagnostics") or [],
                "elapsed_s": item.get("llm_elapsed_s"),
                "token_usage": item.get("token_usage") or {},
                "source": item.get("source") or "",
            }
            for item in compile_attempts
        ]
        orchestrator["context_reports"] = [
            *[
                report
                for attempt in compile_attempts
                for report in (attempt.get("context_reports") or [])
                if isinstance(report, dict)
            ],
            {
                "kind": "coding_review",
                "approved": True,
                "repaired": len(compile_attempts) > 1,
            },
        ]

    dispatches = [
        event for event in events
        if event.get("event") == "master_worker_dispatch" and event.get("kind") == "gui"
    ]
    legacy_dispatches = []
    for event in events:
        if event.get("event") != "master_tool" or event.get("tool") != "run_worker":
            continue
        args = event.get("args") if isinstance(event.get("args"), dict) else {}
        spec = args.get("spec") if isinstance(args.get("spec"), dict) else {}
        legacy_dispatches.append({
            **event,
            "worker_id": str(args.get("worker_id") or "gui_worker"),
            "goal": str(spec.get("goal") or goal),
            "spec": spec,
        })
    starts = [event for event in events if event.get("event") == "worker_started"]
    worker_order: list[str] = []
    worker_meta: dict[str, dict] = {}
    for event in [*dispatches, *legacy_dispatches, *starts]:
        worker_id = str(event.get("worker_id") or "")
        if not worker_id:
            worker_id = "gui_worker"
        if worker_id not in worker_order:
            worker_order.append(worker_id)
        worker_meta.setdefault(worker_id, {}).update(event)

    turns = [event for event in timeline_events if event.get("event") == "worker_turn"]
    if turns and not worker_order:
        fallback_id = str(turns[0].get("worker_id") or "gui_worker")
        worker_order.append(fallback_id)
        worker_meta[fallback_id] = {
            "worker_id": fallback_id,
            "goal": str(turns[0].get("goal") or goal),
            "spec": {},
        }
    if len(worker_order) == 1:
        for turn in turns:
            if not turn.get("worker_id"):
                turn["worker_id"] = worker_order[0]

    worker_results = {
        str(event.get("worker_id") or ""): event
        for event in events if event.get("event") == "master_worker_result"
    }
    terminal_error = next(
        (
            event for event in reversed(events)
            if event.get("event") in {"runtime_error", "runtime_interrupted", "master_program_error"}
        ),
        None,
    )
    pages: list[ReportPage] = []
    statements: list[dict] = []
    run_log: list[dict] = []
    total_turns = 0
    executed = 0
    last_screenshot = first_screenshot

    for worker_id in worker_order:
        meta = worker_meta.get(worker_id) or {}
        spec = meta.get("spec") if isinstance(meta.get("spec"), dict) else {}
        profile = str(spec.get("profile") or meta.get("profile") or "operator")
        worker_goal = str(meta.get("goal") or spec.get("goal") or worker_id)
        criteria = list(spec.get("success_criteria") or meta.get("success_criteria") or [])
        result_event = worker_results.get(worker_id) or {}
        outcome = result_event.get("outcome") if isinstance(result_event.get("outcome"), dict) else {}
        worker_turns = [turn for turn in turns if str(turn.get("worker_id") or "") == worker_id]
        steps: list[ReportStep] = []
        verify_turn: dict | None = None
        for turn in worker_turns:
            state = turn.get("state") if isinstance(turn.get("state"), dict) else {}
            if str(turn.get("tool") or "") == "complete" and state.get("status") == "completed":
                verify_turn = turn
                continue
            ordinal = len(steps) + 1
            frame_id = str(turn.get("frame_id") or "")
            screenshot = frame_screenshots.get(frame_id) or last_screenshot
            if screenshot:
                last_screenshot = screenshot
            decisions = [
                item for item in (turn.get("decisions") or [])
                if isinstance(item, dict)
            ]
            final_decision = decisions[-1] if decisions else {}
            action_batch = compact_action_batch(turn, final_decision)
            action = turn.get("action") if isinstance(turn.get("action"), dict) else {}
            action_event = str(action.get("event") or "")
            if action_batch is not None:
                action_type = "batch"
                operation_mode = "interactive"
            elif action_event == "runtime_action":
                action_type = str(action.get("action_type") or "command")
                operation_mode = "interactive"
            else:
                action_type = runtime_action_types.get(str(turn.get("tool") or "")) or "acquire"
                operation_mode = (
                    "interactive" if action_type != "acquire" else "observation"
                )
            args = turn.get("args") if isinstance(turn.get("args"), dict) else {}
            annotations = (
                [item for item in action_batch["actions"] if item["status"] == "executed"]
                if action_batch is not None
                else [{"action_type": action_type, "args": args, "index": ordinal}]
            )
            annotated, annotated_full = (
                annotated_frame(screenshot, annotations) if screenshot else ("", "")
            )
            timings, token_usage, llm_calls = turn_metrics(turn)
            llm_context = [
                report
                for item in [*(turn.get("details") or []), *(turn.get("decisions") or [])]
                if isinstance(item, dict)
                for report in (item.get("context_reports") or [])
                if isinstance(report, dict)
            ]
            errors = [
                str(item.get("error") or item.get("message") or "")
                for item in (turn.get("errors") or []) if isinstance(item, dict)
            ]
            patches = [
                item.get("action")
                for item in (turn.get("details") or [])
                if isinstance(item, dict) and item.get("event") == "worker_action_patch"
            ]
            non_ui = {
                "executor": "read",
                "goal": "Observation and decision evidence",
                "outputs": {
                    "observation": compact_observation(turn.get("observation") or turn),
                    "state": {
                        "status": state.get("status"),
                        "coverage": state.get("coverage") or {},
                        "established_facts": state.get("established_facts") or [],
                        "open_gaps": state.get("open_gaps") or [],
                    },
                    "action": {
                        "tool": turn.get("tool"),
                        "args": {} if action_batch is not None else args,
                        "status": action.get("status"),
                        "no_effect": action.get("no_effect", False),
                        "data_ref": action.get("data_ref"),
                        "result_ref": action.get("result_ref"),
                        "patches": [item for item in patches if item],
                    },
                    "context": {
                        "assembly": "rebuilt_per_frame",
                        "chars": int(final_decision.get("context_chars") or 0),
                        "journal_events": int(
                            final_decision.get("memory_event_count") or 0
                        ),
                        "state_source": str(
                            final_decision.get("state_source") or "legacy"
                        ),
                        "compatibility": list(
                            final_decision.get("state_compatibility") or []
                        ),
                    },
                    **({"worker_actions": spec.get("actions") or []} if program_event is None else {}),
                },
                "evidence": errors,
            }
            step = ReportStep(
                label=f"Turn {ordinal}",
                action_type=action_type,
                x=float(args["x"]) if isinstance(args.get("x"), (int, float)) else None,
                y=float(args["y"]) if isinstance(args.get("y"), (int, float)) else None,
                description=(
                    f"{state.get('status', 'observing')} · "
                    f"{action_batch['planned']} actions"
                    if action_batch is not None
                    else f"{state.get('status', 'observing')} · "
                    f"{turn.get('tool', 'observe')}"
                ),
                annotated_before_url=annotated,
                annotated_full_url=annotated_full,
                raw_screenshot_url=screenshot,
                status="✗" if turn.get("has_error") else "✓",
                timestamp=event_timestamp(
                    action or (turn.get("decisions") or [turn])[-1]
                ),
                index=int(turn.get("step") or ordinal),
                statement_id=worker_id,
                instance_id=worker_id,
                statement_executor="interact",
                instruction=str(state.get("next_instruction") or ""),
                summary=str(state.get("summary") or turn.get("message") or ""),
                timings=timings,
                token_usage=token_usage,
                llm_calls=llm_calls,
                llm_context=llm_context,
                action_direction=(
                    None if action_batch is not None
                    else str(args.get("direction") or "") or None
                ),
                action_text=(
                    None if action_batch is not None
                    else str(args.get("text") or args.get("description") or "") or None
                ),
                operation_mode=operation_mode,
                non_ui=non_ui,
                no_effect=bool(action.get("no_effect")),
                action_batch=action_batch,
            )
            steps.append(step)
            total_turns += 1
            if not turn.get("has_error"):
                executed += 1

        completed = str(outcome.get("phase") or "") == "completed"
        checklist = [
            {"text": str(item), "status": "done" if completed else "pending"}
            for item in criteria
        ]
        verify_url = ""
        verify_outcome: dict = {}
        outcome_timings: dict[str, float] = {}
        outcome_tokens: dict[str, dict[str, int]] = {}
        if verify_turn is not None:
            frame_id = str(verify_turn.get("frame_id") or "")
            verify_url = frame_screenshots.get(frame_id) or last_screenshot
            if verify_url:
                last_screenshot = verify_url
            verify_state = verify_turn.get("state") if isinstance(verify_turn.get("state"), dict) else {}
            verify_outcome = {
                "status": "done" if completed or verify_state.get("status") == "completed" else "in_progress",
                "reason": str(verify_state.get("summary") or outcome.get("summary") or ""),
                "summary": str(outcome.get("summary") or ""),
            }
            outcome_timings, outcome_tokens, _ = turn_metrics(verify_turn)
        elif outcome:
            verify_outcome = {
                "status": "done" if completed else "failed",
                "reason": str(outcome.get("summary") or ""),
            }
        elif terminal_error is not None:
            verify_outcome = {
                "status": "failed",
                "reason": str(terminal_error.get("message") or ""),
            }
            outcome = {
                "phase": "failed",
                "summary": str(terminal_error.get("message") or ""),
            }

        terminal_note = str(outcome.get("summary") or "")
        description = f"{profile} · {worker_goal}"
        if terminal_note and not steps:
            description += f" · {terminal_note}"

        page = ReportPage(
            title=f"GUI Worker · {worker_id}",
            steps=steps,
            statement_id=worker_id,
            instance_id=worker_id,
            statement_executor="interact",
            statement_name=f"GUI Worker · {worker_id}",
            statement_description=description,
            statement_success="; ".join(map(str, criteria)),
            checklist=checklist,
            verify_url=verify_url,
            verify_outcome=verify_outcome,
            outcome_after_turn=len(steps),
            outcome_timings=outcome_timings,
            outcome_token_usage=outcome_tokens,
        )
        pages.append(page)
        input_tokens = sum(_sum_tokens(step.token_usage)[0] for step in steps) + _sum_tokens(outcome_tokens)[0]
        output_tokens = sum(_sum_tokens(step.token_usage)[1] for step in steps) + _sum_tokens(outcome_tokens)[1]
        total_time = sum(sum(step.timings.values()) for step in steps) + sum(outcome_timings.values())
        statement = {
            "id": worker_id,
            "instance_id": worker_id,
            "name": page.statement_name,
            "executor": "interact",
            "description": page.statement_description,
            "success": page.statement_success,
            "status": str(outcome.get("phase") or raw.get("phase") or ""),
            "phase": str(outcome.get("phase") or raw.get("phase") or ""),
            "checklist": checklist,
            "turns": str(len(steps)),
            "total_time": total_time,
            "timings": {},
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost": sum(_token_cost(step.token_usage) for step in steps) + _token_cost(outcome_tokens),
            "inputs": spec,
            "outputs": {"collection_ref": outcome.get("collection_ref")},
            "last_summary": str(outcome.get("summary") or ""),
        }
        statements.append(statement)
        run_log.append({
            "instance_id": worker_id,
            "node_id": worker_id,
            "name": page.statement_name,
            "executor": "interact",
            "coding_op": "gui_worker",
            "coding_call_id": f"worker:{worker_id}",
            "coding_payload": {
                "worker_id": worker_id,
                **(spec or {"profile": profile, "goal": worker_goal}),
            },
            "result": {
                "phase": str(outcome.get("phase") or raw.get("phase") or ""),
                "summary": str(outcome.get("summary") or ""),
                "outputs": {"collection_ref": outcome.get("collection_ref")},
            },
        })

    transform_starts = [event for event in events if event.get("event") == "transform_started"]
    transform_completes = {
        str(event.get("transform_id") or ""): event
        for event in events if event.get("event") in {"transform_completed", "transform_failed"}
    }
    for start in transform_starts:
        transform_id = str(start.get("transform_id") or "transform")
        complete = transform_completes.get(transform_id) or {}
        completed = complete.get("event") == "transform_completed"
        result_ref = complete.get("result_ref") if isinstance(complete.get("result_ref"), dict) else None
        duration = max(0.0, float(complete.get("elapsed_s") or 0) - float(start.get("elapsed_s") or 0))
        screenshot = last_screenshot
        source_frame = Path(screenshot).stem.rsplit("_", 1)[-1] if screenshot else ""
        step = ReportStep(
            label="Turn 1",
            display_label=(
                f"来源 GUI T{source_frame}" if source_frame.isdigit() else "Runtime"
            ),
            action_type="command",
            x=None,
            y=None,
            description="Deterministic Python data transform",
            annotated_before_url=screenshot,
            annotated_full_url=screenshot,
            raw_screenshot_url=screenshot,
            status="✓" if completed else "✗",
            # This step has an explicit deterministic duration.  Leaving the
            # action timestamp empty prevents the renderer from attributing the
            # GUI Worker's terminal verification gap to this Runtime transform.
            timestamp="",
            index=1,
            statement_id=transform_id,
            instance_id=transform_id,
            statement_executor="command",
            instruction=f"Execute deterministic transform {transform_id}",
            summary=str(complete.get("message") or complete.get("error") or ""),
            timings={"transform": duration} if duration else {},
            operation_mode="non_interactive",
            non_ui={
                "executor": "command",
                "goal": f"Deterministic transform {transform_id}",
                "summary": str(complete.get("message") or complete.get("error") or ""),
                "outputs": {
                    "inputs": start.get("inputs") or [],
                    "result_ref": result_ref,
                },
                "evidence": [str(start.get("source") or "")],
            },
        )
        page = ReportPage(
            title=f"Runtime Transform · {transform_id}",
            steps=[step],
            statement_id=transform_id,
            instance_id=transform_id,
            statement_executor="command",
            statement_name=f"Runtime Transform · {transform_id}",
            statement_description="Deterministic, sandboxed Python data processing",
            statement_success="Produce a schema-validated ResultRef.",
            checklist=[{
                "text": "Deterministic transform produced a schema-valid ResultRef",
                "status": "done" if completed else "blocked",
            }],
        )
        pages.append(page)
        total_turns += 1
        executed += int(completed)
        statement = {
            "id": transform_id,
            "instance_id": transform_id,
            "name": page.statement_name,
            "executor": "command",
            "description": page.statement_description,
            "success": page.statement_success,
            "status": "completed" if completed else "failed",
            "phase": "completed" if completed else "failed",
            "checklist": page.checklist,
            "turns": "1",
            # Deterministic execution time is shown on the card but is not LLM time.
            "total_time": 0.0,
            "timings": {"transform": duration} if duration else {},
            "input_tokens": 0,
            "output_tokens": 0,
            "cost": 0.0,
            "inputs": {
                "transform_id": transform_id,
                "inputs": start.get("inputs") or [],
                "source": start.get("source") or "",
            },
            "outputs": {"result_ref": result_ref},
            "last_summary": str(complete.get("message") or complete.get("error") or ""),
        }
        statements.append(statement)
        run_log.append({
            "instance_id": transform_id,
            "node_id": transform_id,
            "name": page.statement_name,
            "executor": "command",
            "coding_op": "transform",
            "coding_call_id": f"transform:{transform_id}",
            "coding_payload": {
                "transform_id": transform_id,
                "inputs": start.get("inputs") or [],
                "source": start.get("source") or "",
            },
            "result": {
                "phase": "completed" if completed else "failed",
                "summary": str(complete.get("message") or complete.get("error") or ""),
                "outputs": {"result_ref": result_ref},
            },
        })

    if run_log:
        orchestrator["report_run_log"] = run_log
    orchestrator["elapsed_s"] = max(
        (float(event.get("elapsed_s") or 0) for event in events),
        default=0.0,
    )
    orchestrator["settle_s_total"] = sum(
        float(event.get("settle_seconds") or 0)
        for event in events if event.get("event") == "runtime_action"
    )
    return pages, statements, {
        "workers": len(worker_order),
        "turns": total_turns,
        "executed": executed,
    }

def _normalize_error(raw: str | dict | None) -> dict | None:
    """Normalize error to {message, failed_tap?, failed_element?, back_attempts?} or None."""
    if not raw:
        return None
    if isinstance(raw, str):
        return {"message": raw}
    return raw  # already a dict from ProbeAbortedError


def _group_steps_by_statement(
    all_steps: list[ReportStep],
    program_statements: list[dict],
    ms_lookup: dict[str, dict],
) -> list[ReportPage]:
    """Group recorded steps by statement invocation in first-seen order."""
    def _page_for(key: str, steps: list[ReportStep]) -> ReportPage:
        ms_meta = ms_lookup.get(key, {})
        first = steps[0] if steps else None
        return ReportPage(
            title=ms_meta.get("name") or (first.description if first else f"StatementContract {key}"),
            steps=steps,
            statement_id=str(ms_meta.get("id") or (first.statement_id if first else "")),
            instance_id=key,
            statement_executor=ms_meta.get("executor", "") or (first.statement_executor if first else ""),
            statement_name=ms_meta.get("name", "") or (first.description if first else ""),
            statement_description=ms_meta.get("description", "") or (first.summary if first else ""),
            statement_success=ms_meta.get("success", ""),
            checklist=ms_lookup.get(key, {}).get("checklist", []) or [],
        )

    buckets: dict[str, list[ReportStep]] = {}
    first_seen: list[str] = []
    for step in all_steps:
        key = step.instance_id or "_no_statement"
        if key not in buckets:
            buckets[key] = []
            first_seen.append(key)
        buckets[key].append(step)

    pages: list[ReportPage] = []
    emitted: set[str] = set()
    for ms in program_statements:
        key = ms.get("instance_id") or ""
        if not key or key in emitted:
            continue
        emitted.add(key)
        pages.append(_page_for(key, buckets.get(key, [])))
    for key in first_seen:  # orphans → trailing, first-seen order
        if key in emitted:
            continue
        emitted.add(key)
        pages.append(_page_for(key, buckets[key]))
    return pages


# ── Recon builder ─────────────────────────────────────────────

class ReconReportBuilder:
    def build(self, log_dir: Path) -> AppReconData:
        # log_dir is either the app dir (logs/recon/微信) or a single page dir
        if (log_dir / "recon_result.json").exists():
            # Single page directory
            page_dirs = [log_dir]
        else:
            # App directory — iterate its page subdirectories
            page_dirs = sorted(p for p in log_dir.iterdir() if p.is_dir())

        app_name = log_dir.name
        pages: list[ReconPageInfo] = []
        total_taps = 0
        total_navigated = 0

        # Load trace.json early (needed for error lookup during page iteration)
        trace_data: list[dict] | None = None
        trace_path = log_dir / "trace.json"
        if trace_path.exists():
            _raw = json.loads(trace_path.read_text(encoding="utf-8"))
            # Support both old format (list) and new format (dict with pages/transitions)
            trace_data = _raw if isinstance(_raw, list) else _raw.get("pages", [])

        # Index trace errors by page name for quick lookup
        trace_errors: dict[str, str] = {}
        if trace_data:
            for entry in trace_data:
                if entry.get("error"):
                    trace_errors[entry["page"]] = entry["error"]

        for pd in page_dirs:
            initial_path = pd / "initial.png"
            if not initial_path.exists():
                continue  # nothing to show at all

            result_path = pd / "recon_result.json"
            result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else {}

            # Page identity: prefer page_meta.json (exported), fall back to initial_result.json
            page_type = ""
            page_title = pd.name
            description = result.get("description", "")

            meta_path = pd / "page_meta.json"
            if meta_path.exists():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                page_title = meta.get("page_title", pd.name)
                page_type = meta.get("page_type", "")
                description = meta.get("description", description)
            else:
                init_result_path = pd / "initial_result.json"
                if init_result_path.exists():
                    init_data = json.loads(init_result_path.read_text(encoding="utf-8"))
                    description = description or init_data.get("page", {}).get("description", "")

            elements_count = result.get("elements_count", 0)
            signature = ""

            # Annotate screenshot with tap points, save to disk (not embedded in HTML).
            initial_img = _load_img(initial_path)
            raw_taps = result.get("taps", [])
            tap_points = [(t["x"], t["y"], t["index"], t.get("navigated", False)) for t in raw_taps]
            annotated_img = annotate_recon_taps(initial_img, tap_points)
            ann_path = pd / "initial_tap_ann.jpg"
            _save_report_img(annotated_img, ann_path)
            annotated_url = str(ann_path.relative_to(log_dir))

            # Build ReconTap list
            taps: list[ReconTap] = []
            for tap in raw_taps:
                total_taps += 1
                navigated = tap.get("navigated", False)
                if navigated:
                    total_navigated += 1
                tap_path = Path(tap.get("screenshot", ""))
                # Raw tap screenshot already on disk — just use a relative path.
                after_url = str(tap_path.relative_to(log_dir)) if tap_path.is_file() else None

                # Build full back-navigation sequence for navigated taps
                back_seq: list[dict] = []
                if navigated and after_url:
                    tap_idx = tap["index"]
                    tap_dir = pd / "tap"
                    # Step 1: initial page with single tap marker (annotated, save to disk)
                    single_point = [(tap["x"], tap["y"], tap_idx, True)]
                    before_img = annotate_recon_taps(initial_img.copy(), single_point)
                    seq0_path = tap_dir / f"tap_{tap_idx:02d}_seq0.jpg"
                    _save_report_img(before_img, seq0_path)
                    back_seq.append({"src": str(seq0_path.relative_to(log_dir)), "subtitle": "", "success": None})
                    # Step 2: navigated page, annotate with first back-attempt coords if available
                    back_attempts_raw = tap.get("back_attempts", [])
                    if back_attempts_raw and tap_path.is_file():
                        after_img = _load_img(tap_path)
                        after_ann = annotate_back_attempts_img(after_img, [back_attempts_raw[0]])
                        seq1_path = tap_dir / f"tap_{tap_idx:02d}_seq1.jpg"
                        _save_report_img(after_ann, seq1_path)
                        step2_src = str(seq1_path.relative_to(log_dir))
                        first_strategy = back_attempts_raw[0].get('strategy', '')
                        step2_sub = "重新进入子页面" if first_strategy == "forward" else f"回退策略: {first_strategy}"
                    else:
                        step2_src = after_url
                        step2_sub = "已导航"
                    back_seq.append({"src": step2_src, "subtitle": step2_sub, "success": None})
                    # Steps 3+: each back attempt (with screenshot, or retry markers)
                    # Forward steps without a screenshot are collapsed into one terminal
                    # step at the end (they all land on the initial page anyway).
                    pending_forward: list[dict] = []
                    for attempt in tap.get("back_attempts", []):
                        strategy = attempt.get("strategy", "")
                        result_txt = attempt.get("result", "")
                        score = attempt.get("score")
                        score_str = f" {score:.3f}" if score is not None else ""
                        success = attempt.get("success", False)
                        if strategy == "retry":
                            back_seq.append({
                                "src": back_seq[-1]["src"] if back_seq else "",
                                "subtitle": f"↻ {result_txt}{score_str}",
                                "success": None,
                                "is_retry": True,
                            })
                            continue
                        shot = Path(attempt.get("screenshot", ""))
                        if not shot.is_file():
                            if strategy == "forward" and success:
                                pending_forward.append(attempt)
                            continue
                        if strategy == "forward":
                            subtitle = f"{result_txt}（已恢复）"
                        else:
                            subtitle = f"{result_txt}{score_str}"
                        back_seq.append({
                            "src": str(shot.relative_to(log_dir)),
                            "subtitle": subtitle,
                            "success": success,
                        })

                    # Collapse pending no-screenshot forward steps into one terminal step.
                    # Extract the full path: "L0→L1", "L1→L2" → "L0→L1→L2"
                    if pending_forward:
                        steps = [a.get("result", "") for a in pending_forward]
                        levels = [steps[0].split("→")[0]] + [s.split("→")[-1] for s in steps]
                        path_str = "→".join(levels)
                        back_seq.append({
                            "src": str(initial_path.relative_to(log_dir)),
                            "subtitle": f"{path_str}（已恢复）",
                            "success": True,
                        })

                taps.append(ReconTap(
                    index=tap["index"],
                    label=tap.get("label", ""),
                    x=tap.get("x", 0),
                    y=tap.get("y", 0),
                    navigated=navigated,
                    after_url=after_url,
                    back_seq=back_seq,
                    identity=tap.get("identity", {}),
                ))

            # Load knowledge
            knowledge = ""
            knowledge_path = pd / "knowledge.md"
            if knowledge_path.exists():
                knowledge = knowledge_path.read_text(encoding="utf-8")

            page_error = _normalize_error(trace_errors.get(
                pd.name,
                None if result_path.exists() else "探测中断，结果未保存",
            ))

            # Annotate failed-tap screenshot with back-attempt coords
            error_annotated_url = ""
            if page_error:
                back_attempts = page_error.get("back_attempts", [])
                if back_attempts:
                    failed_tap_idx = page_error.get("failed_tap", -1)
                    shot_bytes: bytes | None = None
                    if failed_tap_idx and failed_tap_idx > 0:
                        for tap in raw_taps:
                            if tap.get("index") == failed_tap_idx:
                                tp = Path(tap.get("screenshot", ""))
                                if tp.is_file():
                                    shot_bytes = tp.read_bytes()
                                break
                    if shot_bytes is None:
                        shot_bytes = initial_path.read_bytes()
                    err_img = Image.open(io.BytesIO(shot_bytes)).convert("RGBA")
                    err_img = annotate_back_attempts_img(err_img, back_attempts)
                    err_ann_path = pd / "error_tap_ann.jpg"
                    _save_report_img(err_img, err_ann_path)
                    error_annotated_url = str(err_ann_path.relative_to(log_dir))

            pages.append(ReconPageInfo(
                name=pd.name,
                title=page_title,
                page_type=page_type,
                description=description,
                elements_count=elements_count,
                signature=signature,
                annotated_url=annotated_url,
                taps=taps,
                flows=[],
                knowledge=knowledge,
                error=page_error,
                error_annotated_url=error_annotated_url,
            ))

        # Leaf pages: discovered via parent taps but not probed (depth_limit)
        existing_names = {p.name for p in pages}
        probed_titles = {p.title for p in pages}  # for dedup by title
        dup_warnings: list[dict] = []  # leaf pages skipped due to title match

        # Load leaf title mapping (raw_name → {title, type}) if exported
        leaf_meta_path = log_dir / "leaf_meta.json"
        leaf_meta: dict[str, dict] = {}
        if leaf_meta_path.exists():
            leaf_meta = json.loads(leaf_meta_path.read_text(encoding="utf-8"))

        # Load knowledge files for content lookup. Knowledge lives at the repo root under
        # knowledge/<platform>/<app>/ (recon is iPhone-only today); anchor on this file's repo
        # root rather than log_dir depth, which varies by report type.
        knowledge_dir = _REPO_ROOT / "knowledge" / "iphone" / app_name
        knowledge_files: dict[str, str] = {}  # safe_title → content
        if knowledge_dir.exists():
            for kfile in knowledge_dir.glob("*.md"):
                knowledge_files[kfile.stem] = kfile.read_text(encoding="utf-8")

        for pd in page_dirs:
            result_path = pd / "recon_result.json"
            if not result_path.exists():
                continue
            result = json.loads(result_path.read_text(encoding="utf-8"))
            for tap in result.get("taps", []):
                identity = tap.get("identity") or {}
                is_overlay = identity.get("phase") == "overlay_skip"
                is_depth_limit = tap.get("child_status") == "new_depth_limit"
                if not is_depth_limit and not is_overlay:
                    continue

                if is_overlay:
                    # Overlay taps have no page_name; use composite key
                    meta_key = f"overlay::{pd.name}::{tap.get('label', '')}"
                    leaf_name = meta_key
                    leaf_description = ""
                else:
                    leaf_name = identity.get("page_name", "")
                    if not leaf_name:
                        continue
                    meta_key = leaf_name
                    leaf_description = identity.get("description", "")

                if leaf_name in existing_names:
                    continue
                existing_names.add(leaf_name)

                tap_shot = Path(tap.get("screenshot", ""))
                ann_url = ""
                if tap_shot.is_file():
                    try:
                        ann_url = str(tap_shot.relative_to(log_dir))
                    except ValueError:
                        pass

                # Look up short title from leaf_meta.json (exported)
                meta_entry = leaf_meta.get(meta_key, {})
                leaf_title = meta_entry.get("title", leaf_name if not is_overlay else tap.get("label", "弹窗"))
                leaf_type = meta_entry.get("type", "modal" if is_overlay else "")

                # Skip if same title as a probed page — record as warning
                if leaf_title in probed_titles:
                    dup_warnings.append({
                        "title": leaf_title,
                        "parent": pd.name,
                        "label": tap.get("label", ""),
                        "text_sim": identity.get("text_sim"),
                        "visual_sim": identity.get("visual_sim"),
                    })
                    continue

                # Load knowledge content by matching title to knowledge file
                safe_title = leaf_title.replace("/", "_").replace(" ", "_")
                leaf_knowledge = knowledge_files.get(safe_title, "")
                if not leaf_knowledge and leaf_description:
                    parent_name = result.get("parent_page", "")
                    leaf_knowledge = (
                        f"---\napp: {app_name}\npage_title: {leaf_title}\n"
                        f"page_type: {leaf_type}\nparent_page: {parent_name}\n---\n\n"
                        f"# {leaf_title}\n\n{leaf_description}"
                    )

                pages.append(ReconPageInfo(
                    name=leaf_name,
                    title=leaf_title,
                    page_type="leaf",
                    description=leaf_description,
                    elements_count=0,
                    signature="",
                    annotated_url=ann_url,
                    parent=pd.name,
                    taps=[],
                    flows=[],
                    knowledge=leaf_knowledge,
                    error=None,
                ))

        data = AppReconData(
            app_name=app_name,
            pages=pages,
            stats={
                "pages": len(pages),
                "taps_probed": total_taps,
                "navigated": total_navigated,
                "no_change": total_taps - total_navigated,
            },
            trace=trace_data,
            dup_warnings=dup_warnings,
        )
        return data


# ── Runner builder ─────────────────────────────────────────────

class RunnerReportBuilder:
    def build(self, run_dir: Path) -> ReportData:
        data = ReportData(title=run_dir.name)
        ctx_path = run_dir / "context.json"
        if not ctx_path.exists():
            return data

        ctx = json.loads(ctx_path.read_text(encoding="utf-8"))
        journal = ctx.get("journal") or {}
        journal_events = list(journal.get("events") or [])
        turns = [
            event
            for event in journal_events
            if event.get("event_type") == "turn"
        ]
        statement_views = StatementReportReducer().reduce(
            events=journal_events,
        )
        # Title is the user's ORIGINAL input; the resolved goal is shown as provenance.
        # Old logs without raw_input fall back to the goal.
        data.raw_input = ctx.get("raw_input") or ""
        data.goal = ctx.get("goal", "")
        data.router = ctx.get("router") or {}
        data.platform = ctx.get("platform") or ""
        outcome = ctx.get("outcome") or {}
        data.summary = outcome.get("summary") or ""
        data.phase = outcome.get("phase") or ""
        data.verification = outcome.get("verification") or ""
        data.knowledge = ctx.get("knowledge") or {}
        data.orchestrator = ctx.get("orchestrator") or {}
        outcome_output = str(outcome.get("output") or "")
        if "reply" in ctx:
            data.program_output = outcome_output
            data.reply = str(ctx.get("reply") or "")
        elif outcome_output and outcome_output != data.summary:
            # Legacy chat logs overwrote outcome.output with the Reply.
            data.program_output = data.summary
            data.reply = outcome_output
        else:
            data.program_output = outcome_output or data.summary
        data.webarena = ctx.get("webarena") or {}
        data.mobileworld = ctx.get("mobileworld") or {}
        if data.webarena and not data.webarena.get("eval_result"):
            output_dir = str(data.webarena.get("task_output_dir") or "")
            if output_dir:
                eval_path = Path(output_dir) / "eval_result.json"
                if eval_path.exists():
                    try:
                        data.webarena["eval_result_path"] = str(eval_path)
                        data.webarena["eval_result"] = json.loads(eval_path.read_text(encoding="utf-8"))
                    except Exception:
                        pass
        data.wall_clock_s = ctx.get("wall_clock_s") or 0.0
        data.title = data.raw_input or ctx.get("goal", run_dir.name)

        # Run-level model record; cost is priced against these (not the active config).
        data.models = ctx.get("models", {}) or {}
        _MODELS_MAP.clear()
        _MODELS_MAP.update(data.models)

        if data.orchestrator.get("kind") == "tool_agent":
            data.pages, data.statements, data.stats = _tool_agent_report_steps(
                run_dir,
                data.orchestrator,
                data.goal,
            )
            if not data.wall_clock_s:
                data.wall_clock_s = float(data.orchestrator.get("elapsed_s") or 0)
            data.settle_s_total = float(
                data.orchestrator.get("settle_s_total") or 0
            )
            data.orchestration_summary = (
                "Coding Master → Frozen Reviewed Python → Agentic GUI Workers → Active Perception → "
                "CollectionRefs → Deterministic Runtime Transform → ResultRef"
            )
            if not data.reply:
                data.reply = data.program_output
            return data

        data.settle_s_total = sum((t.get("settle_s") or 0) for t in turns)

        # Sections injected into Transition at least once this run (ordered by first appearance),
        # with bodies read from the knowledge dir so the sidebar can show them on click.
        loaded_order: list[str] = []
        _seen_sec: set[str] = set()
        for _t in turns:
            for _s in (_t.get("sections_loaded") or []):
                if _s not in _seen_sec:
                    _seen_sec.add(_s)
                    loaded_order.append(_s)
        if loaded_order and data.knowledge:
            kdir = (_REPO_ROOT / "knowledge"
                    / (data.platform or "iphone") / str(data.knowledge.get("app_name", "")))
            for stem in loaded_order:
                fp = kdir / f"{stem}.md"
                body = fp.read_text(encoding="utf-8") if fp.exists() else ""
                data.knowledge_sections.append(
                    {"stem": stem, "title": stem.replace("_", " "), "body": body}
                )

        total_actions = 0
        total_executed = 0
        all_steps: list[ReportStep] = []

        for turn in turns:
            idx = turn.get("index", 0)
            operation_mode = str(turn.get("operation_mode") or "interactive")
            non_ui = turn.get("non_ui") if isinstance(turn.get("non_ui"), dict) else None
            ad = turn.get("action_decision") or {}
            action = ad.get("action") or {}
            atype = (non_ui.get("executor") if non_ui else action.get("action_type")) or "none"
            x = action.get("x")
            y = action.get("y")
            desc = (non_ui.get("goal") if non_ui else action.get("description")) or ""
            sup = turn.get("supervisor") or {}
            summary = (non_ui.get("summary") if non_ui else sup.get("summary")) or ""
            executed = bool(turn.get("executed", False))

            total_actions += 1
            if executed:
                total_executed += 1

            ss_name = str(
                (non_ui or {}).get("observation_url")
                or turn.get("observation_url")
                or f"screenshot_turn_{idx}.png"
            )
            ss_path = run_dir / ss_name
            if not ss_path.exists() and operation_mode != "non_interactive":
                for fallback_idx in range(int(idx or 0) - 1, 0, -1):
                    fallback_path = run_dir / f"screenshot_turn_{fallback_idx}.png"
                    if fallback_path.exists():
                        ss_path = fallback_path
                        ss_name = fallback_path.name
                        break
            if ss_path.exists() and x is not None and y is not None:
                img = _load_img(ss_path)
                annotated_img = annotate_action(
                    img, atype, x, y, idx,
                    direction=action.get("direction"),
                    text=action.get("text"),
                    to_x=action.get("to_x"),
                    to_y=action.get("to_y"),
                    snap=action.get("snap"),
                )
                ann_path = run_dir / f"{ss_path.stem}_ann.jpg"
                _save_report_img(annotated_img, ann_path)
                annotated_url = ann_path.name
                # Full-resolution annotated frame for click-to-zoom: the thumbnail uses the
                # downscaled ann.jpg, but zoom shows the action marker at full size (previously
                # zoom fell back to the raw screenshot and dropped the annotation).
                full_ann_path = run_dir / f"{ss_path.stem}_ann_full.jpg"
                _save_report_img(annotated_img, full_ann_path, max_w=None)
                annotated_full_url = full_ann_path.name
            elif ss_path.exists():
                annotated_url = ss_path.name
                annotated_full_url = ss_path.name  # no action coordinate → raw, unannotated
            else:
                annotated_url = ""
                annotated_full_url = ""

            raw_url = ss_path.name if ss_path.exists() else ""

            status = "✓" if executed else "✗"
            if operation_mode == "non_interactive":
                label = {
                    "acquire": "Acquire",
                    "read": "Read",
                    "command": "Command",
                }.get(atype, "non-UI")
                status = f"{'✓' if executed else '✗'} {label}"
            if atype == "none":
                status = "— skip"

            all_steps.append(ReportStep(
                label=f"Turn {idx}",
                action_type=atype,
                x=x,
                y=y,
                description=desc or summary,
                annotated_before_url=annotated_url,
                annotated_full_url=annotated_full_url,
                raw_screenshot_url=raw_url,
                after_url=None,
                status=status,
                timestamp=turn.get("timestamp", ""),
                index=idx,
                statement_id=sup.get("statement_id", ""),
                instance_id=str(turn.get("statement_instance_id") or ""),
                statement_executor=(turn.get("statement") or {}).get("executor", ""),
                instruction=(sup.get("action_intent") or {}).get("instruction", ""),
                summary=summary,
                timings=turn.get("timings", {}),
                token_usage=turn.get("token_usage", {}),
                llm_calls=turn.get("llm_calls", 0),
                action_direction=action.get("direction"),
                action_text=action.get("text"),
                action_to_x=action.get("to_x"),
                action_to_y=action.get("to_y"),
                snap=action.get("snap"),
                sections_loaded=turn.get("sections_loaded") or [],
                llm_context=turn.get("llm_context") or [],
                operation_mode=operation_mode,
                non_ui=non_ui,
                no_effect=bool(turn.get("no_effect")),
            ))

        # Build statement lookup from recorded statement invocations.
        ms_lookup: dict[str, dict] = {}
        statements_static: list[dict] = []
        for view in statement_views:
            key = view.instance_id
            ms_lookup[key] = {
                "id": view.statement_id,
                "instance_id": key,
                "name": view.name,
                "description": view.description,
                "executor": view.executor,
                "success": view.success,
                "status": view.status,
                "acceptance": view.acceptance,
                "checklist": view.checklist,
                "outputs": view.outputs,
                "inputs": view.inputs,
                "call": view.call,
                "last_summary": view.last_summary,
                "pre_existing": view.pre_existing,
                "collection_summary": view.collection_summary,
                "phase": view.phase,
                "verification": view.verification,
                "verification_url": view.verification_url,
                "outcome_after_turn": view.outcome_after_turn,
                "outcome_timings": view.outcome_timings,
                "outcome_token_usage": view.outcome_token_usage,
                "outcome_context": view.outcome_context,
            }
            statements_static.append(ms_lookup[key])

        # Group steps by statement — PROGRAM-ALIGNED when static list exists.
        statements_info: list[dict] = []
        pages = _group_steps_by_statement(
            all_steps, statements_static, ms_lookup
        )

        # Build statements summary
        for page in pages:
            ms_steps = page.steps
            ms_state = ms_lookup.get(page.instance_id, {})
            ms_timings: dict[str, float] = {}
            ms_in = ms_out = 0
            for s in ms_steps:
                for k, v in s.timings.items():
                    ms_timings[k] = ms_timings.get(k, 0) + v
                si, so = _sum_tokens(s.token_usage)
                ms_in += si
                ms_out += so
            for k, v in (ms_state.get("outcome_timings") or {}).items():
                ms_timings[k] = ms_timings.get(k, 0) + v
            outcome_in, outcome_out = _sum_tokens(
                ms_state.get("outcome_token_usage") or {}
            )
            ms_in += outcome_in
            ms_out += outcome_out
            statements_info.append({
                "id": page.statement_id,
                "instance_id": page.instance_id,
                "name": page.statement_name,
                "executor": page.statement_executor,
                "description": page.statement_description,
                "success": page.statement_success,
                "status": ms_state.get("status", ""),
                "outputs": ms_state.get("outputs", {}),
                "inputs": ms_state.get("inputs", {}),
                "call": ms_state.get("call", {}),
                "checklist": ms_state.get("checklist", []),
                "turns": (
                    f"{ms_steps[0].label.split()[-1]}-{ms_steps[-1].label.split()[-1]}"
                    if ms_steps else "—"
                ),
                "total_time": sum(ms_timings.values()),
                "timings": ms_timings,
                "input_tokens": ms_in,
                "output_tokens": ms_out,
                "cost": (
                    sum(_token_cost(s.token_usage) for s in ms_steps)
                    + _token_cost(ms_state.get("outcome_token_usage") or {})
                ),
            })

        # Set the verification screenshot and terminal acceptance projection.
        for i, page in enumerate(pages):
            ms_state = ms_lookup.get(page.instance_id, {})
            page.verify_url = str(ms_state.get("verification_url") or "")
            if not page.verify_url and i + 1 < len(pages):
                next_first = pages[i + 1].steps[0] if pages[i + 1].steps else None
                if next_first and next_first.raw_screenshot_url:
                    page.verify_url = next_first.raw_screenshot_url
            page.verify_outcome = ms_state.get("acceptance", {})
            page.outcome_after_turn = int(ms_state.get("outcome_after_turn") or 0)
            page.outcome_timings = dict(ms_state.get("outcome_timings") or {})
            page.outcome_token_usage = dict(
                ms_state.get("outcome_token_usage") or {}
            )
            page.outcome_context = list(ms_state.get("outcome_context") or [])
        data.pages = pages
        data.statements = statements_info
        # Decompose summary: list all statements with names
        ms_parts = []
        for ms in statements_info:
            ms_parts.append(f"#{ms['id']} {ms['name']}（{ms['executor']}）")
        data.orchestration_summary = " → ".join(ms_parts) if ms_parts else ""
        data.stats = {
            "turns": len(all_steps),
            "executed": total_executed,
        }
        return data
