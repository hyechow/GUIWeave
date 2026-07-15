"""Result assembly and turn-stat printing for agent runs."""

from __future__ import annotations

import time

from llm.structured import get_llm_call_count

from gui_agent.core.schemas import PolicyContext, ProgramPhase, Verification
from gui_agent.core.supervisor.base import SupervisorPolicy

TURN_STATS = "\033[2mTurn {turn_no} stats: llm_calls={llm_calls}, elapsed={elapsed:.2f}s\033[0m"


def make_result(
    context: PolicyContext,
    stop_reason: str,
    collection_context: str | None = None,
    *,
    phase: ProgramPhase = "stopped",
    verification: Verification | None = None,
) -> dict:
    last_summary = context.journal.turns[-1].supervisor.summary if context.journal.turns else stop_reason
    turns_detail = []
    for t in context.journal.turns:
        entry: dict = {"no": t.index, "summary": t.supervisor.summary, "executed": t.executed}
        entry["phase"] = (
            t.supervisor.outcome.phase
            if t.supervisor.outcome is not None
            else "running"
        )
        entry["verification"] = (
            t.supervisor.outcome.verification
            if t.supervisor.outcome is not None
            else None
        )
        if t.action_signal is not None:
            entry["action_signal"] = t.action_signal.model_dump(mode="json")
        if t.action_decision:
            a = t.action_decision.action
            if a is not None:
                entry["action_type"] = a.action_type
                entry["action_desc"] = a.description
            if t.action_decision.not_found_reason:
                entry["not_found"] = t.action_decision.not_found_reason
        turns_detail.append(entry)
    return {
        "goal": context.goal,
        "result_summary": last_summary,
        "stop_reason": stop_reason,
        "phase": phase,
        "verification": verification,
        "turns_count": len(context.journal.turns),
        "turns_detail": turns_detail,
        "task_type": context.task_type,
        "content_notes": context.journal.content_notes or None,
        "collection_context": collection_context,
        "collection_scope": context.collection_scope.model_dump(exclude_none=True)
        if context.collection_scope else None,
    }


def orchestration_result(context, interp, terminal: str, *, current=None) -> dict:
    """Build the final result for DSL orchestrator mode."""

    from gui_agent.core.llm.output import compose_orchestration_reply

    run_log = [r.model_dump() for r in interp.run_log]
    digest = [
        {
            "name": r.name,
            "phase": r.result.phase,
            "verification": r.result.verification,
            "reads": dict(r.result.reads),
            "summary": r.result.summary,
        }
        for r in interp.run_log
    ]
    reply = compose_orchestration_reply(
        context.goal, digest,
        current=(current.name if current is not None else ""),
        terminal=terminal,
    )
    # A program that reached finish but answered on an entirely-empty read produced no real
    # answer (the read found nothing on the frame) — do not let it masquerade as success.
    finish_incomplete = getattr(interp, "finish_incomplete", False)
    accepted_unverified = any(
        r.result.verification == "accepted_unverified"
        for r in interp.run_log
    )
    completed = (
        (current is None)
        and not interp.failed
        and not finish_incomplete
    )
    if completed:
        phase: ProgramPhase = "completed"
        verification: Verification | None = (
            "accepted_unverified" if accepted_unverified else "confirmed"
        )
    elif interp.failed or finish_incomplete:
        phase = "failed"
        verification = None
    elif "ESC" in terminal or "用户退出" in terminal or "用户按" in terminal:
        phase = "interrupted"
        verification = None
    else:
        phase = "stopped"
        verification = None
    base = make_result(
        context,
        terminal,
        phase=phase,
        verification=verification,
    )
    base["result_summary"] = reply
    base["orchestrator"] = {
        "reply": reply,
        "terminal": terminal,
        "run_log": run_log,
    }
    return base


def print_turn_stats(turn_no: int, started_at: float, llm_calls_before: int) -> None:
    elapsed = time.perf_counter() - started_at
    llm_calls = get_llm_call_count() - llm_calls_before
    print(TURN_STATS.format(turn_no=turn_no, llm_calls=llm_calls, elapsed=elapsed))


def print_timings(supervisor: SupervisorPolicy) -> None:
    timings = getattr(supervisor, "_timings", None)
    order = getattr(supervisor, "_timings_order", None)
    if not timings or not order:
        return
    parts = [f"{n}={timings[n]:.2f}s" for n in order]
    total = sum(timings.values())
    print(f"  [Timing] {' | '.join(parts)} | total={total:.2f}s")
