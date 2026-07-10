"""Control-flow helpers for the agent loop."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from gui_agent.core.run.result import make_result, orchestration_result
from gui_agent.core.schemas import PolicyContext, SupervisorStep


@dataclass
class LoadingResult:
    streak: int
    continue_loop: bool = False
    terminal_result: dict | None = None


def handle_loading_frame(
    *,
    loading_streak: int,
    max_loading_frames: int,
    wait_s: float,
    turn_no: int,
    program: Any,
    current_run: Any,
    context: PolicyContext,
    interpreter: Any,
    finish: Callable[[dict], dict],
    stop_after_esc: Callable[[int], dict | None],
    say: Callable[[str], None],
) -> LoadingResult:
    """Handle a loading frame without consuming a turn budget."""
    next_streak = loading_streak + 1
    if next_streak > max_loading_frames:
        say(f"\n页面持续加载 {next_streak} 帧仍未稳定，agent-loop 停止")
        term = f"页面持续加载未稳定（>{max_loading_frames} 帧）"
        if program is not None:
            return LoadingResult(
                streak=next_streak,
                terminal_result=finish(orchestration_result(context, interpreter, term, current=current_run)),
            )
        return LoadingResult(streak=next_streak, terminal_result=finish(make_result(context, term)))

    say(f"  [Loading] 等待页面稳定（第 {next_streak} 帧，不计入轮数）...")
    time.sleep(wait_s)
    interrupted = stop_after_esc(turn_no)
    if interrupted is not None:
        return LoadingResult(streak=next_streak, terminal_result=interrupted)
    return LoadingResult(streak=next_streak, continue_loop=True)


@dataclass
class ProgressDecision:
    noop_count: int
    prev_milestone_id: str | None
    continue_loop: bool = False
    stop_reason: str | None = None
    message: str | None = None
    stop_message: str | None = None


def evaluate_turn_progress(
    *,
    noop_count: int,
    prev_milestone_id: str | None,
    sv_step: SupervisorStep,
    executed: bool,
    action_decision: Any,
    probe_failed: bool,
) -> ProgressDecision:
    """Update noop accounting and decide whether the loop should continue or stop."""
    if not executed and sv_step.should_act:
        if probe_failed:
            return _increment_or_stop(
                noop_count,
                stop_kind="滚动探测失败",
                continue_message="滚动探测失败，进入下一轮重新规划",
            )
        if action_decision and action_decision.not_found_reason:
            return _increment_or_stop(noop_count, stop_kind="无动作")
        if action_decision is not None and getattr(action_decision, "action", None) is not None:
            return _increment_or_stop(
                noop_count,
                stop_kind="动作执行失败",
                continue_message="动作执行失败，进入下一轮重新规划",
            )
        return ProgressDecision(
            noop_count=noop_count,
            prev_milestone_id=prev_milestone_id,
            stop_reason="动作未执行，agent-loop 停止",
        )

    if sv_step.milestone_id != prev_milestone_id:
        noop_count = 0
    prev_milestone_id = sv_step.milestone_id

    if not sv_step.should_act:
        return _increment_or_stop(
            noop_count,
            prev_milestone_id=prev_milestone_id,
            stop_kind="无动作",
        )

    return ProgressDecision(noop_count=0, prev_milestone_id=prev_milestone_id)


def _increment_or_stop(
    noop_count: int,
    *,
    stop_kind: str,
    prev_milestone_id: str | None = None,
    continue_message: str | None = None,
) -> ProgressDecision:
    next_count = noop_count + 1
    if next_count >= 3:
        reason = f"连续 {next_count} 轮{stop_kind}"
        return ProgressDecision(
            noop_count=next_count,
            prev_milestone_id=prev_milestone_id,
            stop_reason=reason,
            stop_message=f"\n{reason}，agent-loop 停止",
        )
    return ProgressDecision(
        noop_count=next_count,
        prev_milestone_id=prev_milestone_id,
        continue_loop=True,
        message=continue_message,
    )


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
        from gui_agent.core.orchestrator.runner import make_run_result

        result = make_run_result(
            current_run,
            completed=sv_step.goal_completed,
            summary=sv_step.summary or reason,
            notes=context.content_notes[notes_mark:],
            completion_status=sv_step.completion_status,
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
