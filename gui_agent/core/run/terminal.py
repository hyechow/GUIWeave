"""Terminal turn handling for the agent loop."""

from __future__ import annotations

from typing import Any, Callable

from gui_agent.core.run.result import make_result, orchestration_result
from gui_agent.core.schemas import PolicyContext, SupervisorStep


def finish_terminal_step(
    *,
    sv_step: SupervisorStep,
    read_state: Any,
    turn_no: int,
    program: Any,
    current_run: Any,
    interpreter_steps: Any,
    interpreter: Any,
    context: PolicyContext,
    notes_mark: int,
    finish: Callable[[dict], dict],
    say: Callable[[str], None],
) -> dict:
    """Flush reads and build the terminal result for a stop/completed step."""
    reason = sv_step.stop_reason or ("目标已达成" if sv_step.goal_completed else "agent-loop 停止")
    read_state.drain_pending(say=say)
    read_state.flush(turn_no=turn_no, say=say)
    if sv_step.goal_completed:
        say(f"\n目标已达成：{reason}")
    else:
        say(f"\n任务未完成：{reason}")

    if program is not None:
        from gui_agent.core.orchestrator.engine import package_result

        result = package_result(
            current_run,
            completed=False,
            summary=sv_step.summary or reason,
            notes=context.content_notes[notes_mark:],
        )
        try:
            next_run = interpreter_steps.send(result)
        except StopIteration as exc:
            return finish(orchestration_result(context, interpreter, exc.value or ""))
        return finish(
            orchestration_result(
                context,
                interpreter,
                sv_step.summary or reason,
                current=next_run,
            )
        )

    if sv_step.goal_completed:
        return finish(make_result(context, reason, sv_step.collection_summary))
    return finish(make_result(context, reason))
