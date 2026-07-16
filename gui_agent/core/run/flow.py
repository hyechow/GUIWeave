"""Control-flow helpers for the agent loop."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from gui_agent.core.run.result import AgentResult, orchestration_result
from gui_agent.core.schemas import PolicyContext, SupervisorStep


@dataclass
class LoadingResult:
    streak: int
    continue_loop: bool = False
    terminal_result: AgentResult | None = None


def handle_loading_frame(
    *,
    loading_streak: int,
    max_loading_frames: int,
    wait_s: float,
    turn_no: int,
    current_run: Any,
    context: PolicyContext,
    interpreter: Any,
    finish: Callable[[AgentResult], AgentResult],
    stop_after_esc: Callable[[int], AgentResult | None],
    say: Callable[[str], None],
) -> LoadingResult:
    """Handle a loading frame without consuming a turn budget."""
    next_streak = loading_streak + 1
    if next_streak > max_loading_frames:
        say(f"\n页面持续加载 {next_streak} 帧仍未稳定，agent-loop 停止")
        term = f"页面持续加载未稳定（>{max_loading_frames} 帧）"
        return LoadingResult(
            streak=next_streak,
            terminal_result=finish(orchestration_result(context, interpreter, term, current=current_run)),
        )

    say(f"  [Loading] 等待页面稳定（第 {next_streak} 帧，不计入轮数）...")
    time.sleep(wait_s)
    interrupted = stop_after_esc(turn_no)
    if interrupted is not None:
        return LoadingResult(streak=next_streak, terminal_result=interrupted)
    return LoadingResult(streak=next_streak, continue_loop=True)


@dataclass
class ProgressDecision:
    stop_reason: str | None = None
    stop_message: str | None = None


def evaluate_turn_progress(
    *,
    sv_step: SupervisorStep,
    executed: bool,
    action_decision: Any,
    suppressed_reason: str = "",
) -> ProgressDecision:
    """Enforce the minimal kernel invariant: a running turn must dispatch an action."""
    if not executed and sv_step.should_act:
        if suppressed_reason:
            reason = f"动作被执行协议抑制：{suppressed_reason}"
            return ProgressDecision(
                stop_reason=reason,
                stop_message=f"\n{reason}，agent-loop 停止",
            )
        if action_decision and action_decision.not_found_reason:
            reason = f"动作目标未找到：{action_decision.not_found_reason}"
            return ProgressDecision(
                stop_reason=reason,
                stop_message=f"\n{reason}，agent-loop 停止",
            )
        if action_decision is not None and getattr(action_decision, "action", None) is not None:
            reason = "动作执行失败"
            return ProgressDecision(
                stop_reason=reason,
                stop_message=f"\n{reason}，agent-loop 停止",
            )
        return ProgressDecision(
            stop_reason="动作未执行，agent-loop 停止",
        )

    if not sv_step.should_act:
        return ProgressDecision(
            stop_reason="运行中的 Statement 未产生动作或终态",
            stop_message="\n运行中的 Statement 未产生动作或终态，agent-loop 停止",
        )

    return ProgressDecision()


def finish_terminal_step(
    *,
    outcome: Any,
    read_state: Any,
    turn_no: int,
    program_runtime: Any,
    context: PolicyContext,
    finish: Callable[[AgentResult], AgentResult],
    say: Callable[[str], None],
    end_statement: Callable[[Any], None],
) -> AgentResult:
    """Flush reads and send the authoritative terminal outcome into ProgramRuntime."""
    reason = outcome.summary or "statement stopped"
    read_state.drain_pending(say=say)
    read_state.flush(turn_no=turn_no, say=say)

    if outcome.is_completed:
        say(f"\n目标已达成：{outcome.summary or reason}")
    else:
        say(f"\n任务未完成：{outcome.summary or reason}")

    try:
        program_runtime.send_outcome(outcome)
    finally:
        end_statement(outcome)
    if program_runtime.finished:
        return finish(
            orchestration_result(
                context,
                program_runtime.interpreter,
                program_runtime.reply or reason,
            )
        )
    return finish(
        orchestration_result(
            context,
            program_runtime.interpreter,
            reason,
            current=program_runtime.current,
        )
    )
