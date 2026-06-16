"""Persistence helpers for run and milestone runtime state."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from gui_agent.core.schemas import (
    MilestoneChecklistItem,
    MilestoneState,
    PolicyContext,
    RunState,
)

_RUNTIME_MILESTONE_KEYS = {
    "status",
    "retry_count",
    "done_check",
    "checklist",
    "reads",
}


def strip_legacy_milestone_runtime_fields(milestones: list[dict]) -> None:
    """Keep context.milestones as the static decomposition shape."""
    for ms in milestones:
        if isinstance(ms, dict):
            for key in _RUNTIME_MILESTONE_KEYS:
                ms.pop(key, None)


def classify_run_status(result: dict) -> str:
    """Classify a finished run for reports without relying on LLM wording."""
    if result.get("goal_completed"):
        return "completed"
    stop_reason = str(result.get("stop_reason") or "")
    if "ESC" in stop_reason or "用户退出" in stop_reason or "用户按" in stop_reason:
        return "interrupted"
    return "stopped"


def run_state_from_result(result: dict, output: str | None = None) -> RunState:
    return RunState(
        status=classify_run_status(result),
        stop_reason=str(result.get("stop_reason") or ""),
        goal_completed=bool(result.get("goal_completed", False)),
        output=output,
    )


def sync_context_run_state(
    context: PolicyContext,
    result: dict,
    output: str | None = None,
) -> None:
    run_state = run_state_from_result(result, output=output)
    context.run = run_state
    context.output = run_state.output
    context.stop_reason = run_state.stop_reason or None
    context.run_status = run_state.status
    context.goal_completed = run_state.goal_completed


def write_final_run_state(context_path: Path, result: dict, output: str) -> None:
    """Patch final run state without reloading PolicyContext.

    PolicyContext round-tripping can fail on platform-specific action subclasses in
    old turn records. Keep this as the only raw JSON patch site for final run state.
    """
    raw = json.loads(context_path.read_text(encoding="utf-8"))
    run_state = run_state_from_result(result, output)
    existing_run = raw.get("run") if isinstance(raw.get("run"), dict) else {}
    raw["run"] = {
        **existing_run,
        **run_state.model_dump(mode="json"),
    }
    # Back-compat for existing report/output consumers and older tooling.
    raw["output"] = run_state.output
    raw["stop_reason"] = run_state.stop_reason
    raw["run_status"] = run_state.status
    raw["goal_completed"] = run_state.goal_completed
    context_path.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _milestone_state_for(context: PolicyContext, milestone_id: str) -> MilestoneState:
    state = context.milestone_states.get(milestone_id)
    if state is None:
        state = MilestoneState(id=milestone_id)
        context.milestone_states[milestone_id] = state
    return state


def _checklist_item_id(prefix: str, text: str) -> str:
    normalized = re.sub(r"\s+", "", text.strip().lower())
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:10]
    return f"{prefix}-{digest}"


def _success_checklist_texts(success_condition: str, fallback: str = "") -> list[str]:
    source = (success_condition or fallback or "完成当前子目标").strip()
    parts = [p.strip(" \t\r\n-•*") for p in re.split(r"[\n;；]+", source)]
    parts = [p for p in parts if p]
    return parts[:8] or [source]


def _upsert_checklist_item(
    state: MilestoneState,
    *,
    item_id: str,
    text: str,
    status: str,
    evidence: list[str] | None = None,
    source: str,
) -> None:
    item = next((i for i in state.checklist if i.id == item_id), None)
    clean_evidence = [e for e in (evidence or []) if e]
    if item is None:
        state.checklist.append(
            MilestoneChecklistItem(
                id=item_id,
                text=text,
                status=status,  # type: ignore[arg-type]
                evidence=clean_evidence,
                source=source,
            )
        )
        return
    item.text = text
    if item.status == "done" and status != "done":
        status = "done"
    item.status = status  # type: ignore[assignment]
    if clean_evidence:
        item.evidence = clean_evidence
    item.source = source


def _update_checklist_from_checker(
    state: MilestoneState,
    *,
    success_condition: str,
    fallback: str,
    checker: dict,
) -> None:
    """Derive milestone checklist state from the checker contract."""
    status = str(checker.get("status") or "")
    reason = str(checker.get("reason") or "")
    visible = [str(v) for v in (checker.get("visible_evidence") or []) if str(v)]
    missing = [str(v) for v in (checker.get("missing_evidence") or []) if str(v)]

    if status == "done":
        item_status = "done"
    elif status == "stuck":
        item_status = "blocked"
    else:
        item_status = "pending"

    evidence = visible or ([reason] if reason else [])
    for text in _success_checklist_texts(success_condition, fallback):
        _upsert_checklist_item(
            state,
            item_id=_checklist_item_id("accept", text),
            text=text,
            status=item_status,
            evidence=evidence,
            source="checker:success_condition",
        )

    missing_status = "blocked" if status == "stuck" else "pending"
    for text in missing[:8]:
        _upsert_checklist_item(
            state,
            item_id=_checklist_item_id("missing", text),
            text=text,
            status=missing_status,
            evidence=[reason] if reason else [],
            source="checker:missing_evidence",
        )

    if status == "done":
        for item in state.checklist:
            if item.source == "checker:missing_evidence" and item.status != "done":
                item.status = "done"
                if reason:
                    item.evidence = [reason]


def _snapshot_from_supervisor(supervisor: object) -> dict:
    snapshot = getattr(supervisor, "runtime_state_snapshot", None)
    if callable(snapshot):
        data = snapshot()
        return data if isinstance(data, dict) else {}

    milestones: dict[str, dict[str, Any]] = {}
    for mid, milestone in (getattr(supervisor, "_milestones", {}) or {}).items():
        milestones[str(mid)] = {
            "status": getattr(milestone, "status", None),
            "retry_count": getattr(milestone, "retry_count", None),
        }
    done_checks: dict[str, dict] = {}
    for mid, check in (getattr(supervisor, "_milestone_done_checks", {}) or {}).items():
        if hasattr(check, "model_dump"):
            done_checks[str(mid)] = check.model_dump(mode="json", exclude_none=True)
        elif isinstance(check, dict):
            done_checks[str(mid)] = dict(check)
    return {
        "milestones": milestones,
        "done_checks": done_checks,
        "last_page_identity": dict(getattr(supervisor, "_last_page_identity", {}) or {}),
        "scroll_counts": dict(getattr(supervisor, "_scroll_counts", {}) or {}),
        "progress_values": dict(getattr(supervisor, "_progress_values", {}) or {}),
    }


def _sync_orchestrator_milestone_states(context: PolicyContext) -> None:
    orchestrator = context.orchestrator if isinstance(context.orchestrator, dict) else {}
    run_log = orchestrator.get("run_log") or []
    if not isinstance(run_log, list):
        return

    ids_by_name: dict[str, list[str]] = {}
    for ms in context.milestones:
        mid = str(ms.get("id") or "")
        name = str(ms.get("name") or "")
        if mid and name:
            ids_by_name.setdefault(name, []).append(mid)
    seen_by_name: dict[str, int] = {}

    for record in run_log:
        if not isinstance(record, dict):
            continue
        name = str(record.get("name") or "")
        mid = str(record.get("var") or "")
        if not mid and name:
            candidates = ids_by_name.get(name, [])
            offset = seen_by_name.get(name, 0)
            if offset < len(candidates):
                mid = candidates[offset]
                seen_by_name[name] = offset + 1
        if not mid:
            continue
        result = record.get("result") if isinstance(record.get("result"), dict) else {}
        state = _milestone_state_for(context, mid)
        if result.get("failed"):
            state.status = "failed"
        elif result.get("completed"):
            state.status = "done"
        reads = result.get("reads") if isinstance(result.get("reads"), dict) else {}
        if reads:
            state.reads = {str(k): str(v) for k, v in reads.items()}
        summary = str(result.get("summary") or "")
        if summary:
            state.last_summary = summary


def sync_milestone_states(supervisor: object, context: PolicyContext) -> None:
    """Persist milestone runtime state in context.milestone_states."""
    static_by_id: dict[str, dict] = {}
    for ms in context.milestones:
        mid = str(ms.get("id") or "")
        if mid:
            static_by_id[mid] = ms
            _milestone_state_for(context, mid)

    snapshot = _snapshot_from_supervisor(supervisor)
    for mid, milestone_state in (snapshot.get("milestones") or {}).items():
        state = _milestone_state_for(context, str(mid))
        status = milestone_state.get("status") if isinstance(milestone_state, dict) else None
        if status in {"pending", "running", "done", "failed"}:
            state.status = status
        retry_count = milestone_state.get("retry_count") if isinstance(milestone_state, dict) else None
        if isinstance(retry_count, int):
            state.retry_count = retry_count

    for mid, check in (snapshot.get("done_checks") or {}).items():
        state = _milestone_state_for(context, str(mid))
        if isinstance(check, dict):
            state.done_check = dict(check)
        ms = static_by_id.get(str(mid), {})
        if state.done_check:
            _update_checklist_from_checker(
                state,
                success_condition=str(ms.get("success_condition") or ""),
                fallback=str(ms.get("name") or mid),
                checker=state.done_check,
            )

    for mid, page_identity in (snapshot.get("last_page_identity") or {}).items():
        state = _milestone_state_for(context, str(mid))
        state.last_page_identity = str(page_identity or "")
    for mid, count in (snapshot.get("scroll_counts") or {}).items():
        state = _milestone_state_for(context, str(mid))
        if isinstance(count, int):
            state.scroll_count = count
    for mid, values in (snapshot.get("progress_values") or {}).items():
        state = _milestone_state_for(context, str(mid))
        if isinstance(values, list):
            state.progress_values = [str(v) for v in values]

    for turn in context.turns:
        sv = turn.supervisor
        mid = sv.milestone_id or ""
        if not mid:
            continue
        state = _milestone_state_for(context, mid)
        state.last_turn_index = turn.index
        state.last_summary = sv.summary or state.last_summary
        if turn.read_note_hash and turn.read_note_hash not in state.note_hashes:
            state.note_hashes.append(turn.read_note_hash)
        ms = static_by_id.get(mid, {})
        if turn.checker:
            _update_checklist_from_checker(
                state,
                success_condition=str(ms.get("success_condition") or ""),
                fallback=str(ms.get("name") or mid),
                checker=turn.checker,
            )
        if sv.pre_existing:
            state.pre_existing = True
        if sv.collection_summary:
            state.collection_summary = sv.collection_summary
        if sv.collection_scope:
            state.collection_scope = sv.collection_scope

    _sync_orchestrator_milestone_states(context)
    strip_legacy_milestone_runtime_fields(context.milestones)
