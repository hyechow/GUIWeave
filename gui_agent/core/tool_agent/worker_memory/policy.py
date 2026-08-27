"""Deterministic receipt construction and per-action outcome classification."""

from __future__ import annotations

from typing import Any

from .journal import ActionOutcome, ActionReceipt, TargetRef, WorkerJournalEvent


_SPATIAL_ARGS = {"x", "y", "to_x", "to_y", "target_ref"}
_RESULT_FIELDS = (
    "status", "action_type", "no_effect", "kind", "ref", "requirement_id",
    "row_count", "coverage", "summary", "reason", "error", "recovery",
    "platform_feedback", "target_signal",
)


def _semantic_args(args: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in args.items() if key not in _SPATIAL_ARGS}


def _semantic_result(result: Any) -> Any:
    if not isinstance(result, dict):
        return result
    return {
        key: result[key] for key in _RESULT_FIELDS
        if key in result and result[key] not in (None, "", [], {})
    }


def _action_outcome(result: Any) -> ActionOutcome:
    if not isinstance(result, dict):
        return ActionOutcome("invoked", detail=result)
    action_type = str(result.get("action_type") or "")
    signal = result.get("target_signal")
    signal = signal if isinstance(signal, dict) else {}
    target = str(signal.get("actual_element") or "").strip()
    detail = _semantic_result(result)
    if result.get("error") or result.get("status") in {"error", "failed"}:
        kind = "failed"
    elif signal.get("status") == "off_target":
        kind = "off_target"
    elif result.get("no_effect"):
        kind = "no_effect"
    elif result.get("candidate_commit") or signal.get("status") == "on_target":
        kind = "effect"
    elif result.get("kind") == "result" or str(result.get("ref") or "").startswith("result:"):
        kind = "effect"
    else:
        kind = "invoked"
    return ActionOutcome(kind, action_type=action_type, target=target, detail=detail)


def action_receipt_event(
    *,
    worker_id: str,
    step: int,
    frame_id: str,
    tool: str,
    args: dict[str, Any],
    result: Any,
    commitment_refs: tuple[str, ...],
    target_ref: str = "",
    state_target_ref: str = "",
    target: TargetRef | None = None,
    substep: int | None = None,
) -> WorkerJournalEvent:
    candidate_commit = isinstance(result, dict) and bool(result.get("candidate_commit"))
    event_ref = f"step:{step}.{substep}" if substep is not None else f"step:{step}"
    outcome = _action_outcome(result)
    clears_target = bool(
        isinstance(result, dict)
        and result.get("status") == "executed"
        and result.get("no_effect") is False
        and outcome.action_type in {
            "scroll", "drag", "navigate", "open_url", "home", "app_switch", "launch_app",
        }
    )
    receipt = ActionReceipt(
        tool=tool, args=_semantic_args(args), outcome=outcome,
        commitment_refs=commitment_refs,
        executed=isinstance(result, dict) and result.get("status") == "executed",
        target_ref=target_ref, state_target_ref=state_target_ref, target=target,
        clears_target=clears_target,
    )
    return WorkerJournalEvent(
        event_ref=event_ref,
        kind="candidate_commit" if candidate_commit else "action_receipt",
        frame_id=frame_id, attempt_id=worker_id, receipt=receipt,
    )
