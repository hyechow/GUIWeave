"""Turn progress/no-op accounting."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gui_agent.core.schemas import SupervisorStep


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
