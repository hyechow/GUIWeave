"""Interactive turn recording helpers."""

from __future__ import annotations

from typing import Any, Callable

from llm.structured import get_llm_call_count, get_llm_token_usage

from gui_agent.core.run.context import extract_checker, extract_plan, extract_replan
from gui_agent.core.run.result import print_timings, print_turn_stats
from gui_agent.core.run.state import sync_milestone_states
from gui_agent.core.run.turns import make_interactive_turn
from gui_agent.core.schemas import PolicyContext, SupervisorStep


def record_interactive_turn(
    *,
    context: PolicyContext,
    observation_source: str,
    supervisor_step: SupervisorStep,
    supervisor: Any,
    action_decision: Any,
    executed: bool,
    llm_calls_before: int,
    tokens_before: tuple[int, int],
    turn_started_at: float,
    read_added_content: bool,
    read_note_hash: str | None,
    save_context: Callable[[], None],
    silent: bool,
    on_turn: Any = None,
) -> Any:
    """Append the UI turn, sync persisted state, and notify the optional callback."""
    tokens_after = get_llm_token_usage()
    turn = make_interactive_turn(
        index=len(context.turns) + 1,
        observation_source=observation_source,
        supervisor_step=supervisor_step,
        action_decision=action_decision,
        checker=extract_checker(supervisor),
        planner=extract_plan(supervisor),
        replan=extract_replan(supervisor),
        executed=executed,
        llm_calls=get_llm_call_count() - llm_calls_before,
        input_tokens=tokens_after[0] - tokens_before[0],
        output_tokens=tokens_after[1] - tokens_before[1],
        read_added_content=read_added_content,
        read_note_hash=read_note_hash,
        timings=getattr(supervisor, "_timings", {}),
        token_usage=getattr(supervisor, "_token_usage", {}),
        sections_loaded=list(getattr(supervisor, "_last_sections_loaded", []) or []),
    )
    print_timings(supervisor)
    context.turns.append(turn)
    sync_milestone_states(supervisor, context)
    save_context()
    if not silent:
        print_turn_stats(turn.index, turn_started_at, llm_calls_before)
    if on_turn and callable(on_turn):
        on_turn(turn_callback_entry(turn, supervisor_step, action_decision, executed))
    return turn


def turn_callback_entry(
    turn,
    supervisor_step: SupervisorStep,
    action_decision: Any,
    executed: bool,
) -> dict:
    """Build the compact callback payload emitted after a turn is recorded."""
    entry: dict = {"no": turn.index, "summary": supervisor_step.summary, "executed": executed}
    if action_decision:
        action = action_decision.action
        entry["action_type"] = action.action_type
        entry["action_desc"] = action.description
        if action_decision.not_found_reason:
            entry["not_found"] = action_decision.not_found_reason
    return entry
