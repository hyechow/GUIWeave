"""Persistence helpers for run and milestone runtime state."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from gui_agent.core.schemas import (
    MilestoneChecklistItem,
    MilestoneState,
    PolicyContext,
    RunState,
    split_acceptance_items,
)

_RUNTIME_MILESTONE_KEYS = {
    "status",
    "retry_count",
    "done_check",
    "checklist",
    "reads",
}


def strip_milestone_runtime_fields(milestones: list[dict]) -> None:
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
    for key in ("output", "stop_reason", "run_status", "goal_completed"):
        raw.pop(key, None)
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
    # Shared with the checker (model_io.run_checker enumerates the same items), so item index
    # in item_verdicts lines up with this ordering.
    return split_acceptance_items(success_condition, fallback)


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
    """Derive milestone checklist state from the checker contract.

    Two kinds of items:
    - Acceptance items (success_condition split), each judged independently via
      ``checker.item_verdicts`` (own status + evidence); without verdicts they fall back to the
      shared whole-milestone verdict.
    - The checker's per-turn ``missing_evidence`` (what's still missing), accumulated across turns
      as an audit trail. Kept at their TRUE status (pending/blocked) — NOT force-marked done on
      completion (that read as misleading "✓ 未选择文件")."""
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
    # Per-item verdicts (checker.item_verdicts) give each acceptance item its OWN status + evidence.
    # When absent (loop/synthetic checks, older runs) fall back to the shared whole-milestone verdict.
    verdicts: dict[int, dict] = {}
    for v in (checker.get("item_verdicts") or []):
        if isinstance(v, dict) and isinstance(v.get("index"), int):
            verdicts[v["index"]] = v
    for idx, text in enumerate(_success_checklist_texts(success_condition, fallback), 1):
        v = verdicts.get(idx)
        if v is not None:
            met = bool(v.get("met"))
            item_status_i = "done" if met else ("blocked" if status == "stuck" else "pending")
            ev = str(v.get("evidence") or "").strip()
            item_evidence_i = [ev] if ev else (evidence if not met else [])
            source_i = "checker:item_verdict"
        else:
            item_status_i = item_status
            item_evidence_i = evidence
            source_i = "checker:success_condition"
        _upsert_checklist_item(
            state,
            item_id=_checklist_item_id("accept", text),
            text=text,
            status=item_status_i,
            evidence=item_evidence_i,
            source=source_i,
        )

    # Per-turn missing_evidence → checklist rows, kept at their true status (no force-✓ on done).
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

    snapshot = supervisor.runtime_state_snapshot()
    if not isinstance(snapshot, dict):
        snapshot = {}
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
    strip_milestone_runtime_fields(context.milestones)
