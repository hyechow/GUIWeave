"""Result assembly and turn-stat printing for agent runs."""

from __future__ import annotations

import time

from llm.structured import get_llm_call_count

from gui_agent.core.schemas import PolicyContext
from gui_agent.core.supervisor.base import SupervisorPolicy

TURN_STATS = "\033[2mTurn {turn_no} stats: llm_calls={llm_calls}, elapsed={elapsed:.2f}s\033[0m"


def make_result(
    context: PolicyContext,
    stop_reason: str,
    collection_context: str | None = None,
) -> dict:
    last_summary = context.turns[-1].supervisor.summary if context.turns else stop_reason
    turns_detail = []
    for t in context.turns:
        entry: dict = {"no": t.index, "summary": t.supervisor.summary, "executed": t.executed}
        if t.action_decision:
            a = t.action_decision.action
            entry["action_type"] = a.action_type
            entry["action_desc"] = a.description
            if t.action_decision.not_found_reason:
                entry["not_found"] = t.action_decision.not_found_reason
        turns_detail.append(entry)
    return {
        "goal": context.goal,
        "result_summary": last_summary,
        "stop_reason": stop_reason,
        "goal_completed": any(t.supervisor.goal_completed for t in context.turns),
        "turns_count": len(context.turns),
        "turns_detail": turns_detail,
        "task_type": context.task_type,
        "content_notes": context.content_notes or None,
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
            "completed": r.result.completed,
            "failed": r.result.failed,
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
    base = make_result(context, terminal)
    base["result_summary"] = reply
    # A program that reached finish but answered on an entirely-empty read produced no real
    # answer (the read found nothing on the frame) — do not let it masquerade as success.
    finish_incomplete = getattr(interp, "finish_incomplete", False)
    base["goal_completed"] = (current is None) and not interp.failed and not finish_incomplete
    base["orchestrator"] = {
        "reply": reply,
        "failed": interp.failed,
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
