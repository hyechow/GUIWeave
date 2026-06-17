"""Loading-frame handling for the agent loop."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from gui_agent.core.run.result import make_result, orchestration_result
from gui_agent.core.schemas import PolicyContext


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
