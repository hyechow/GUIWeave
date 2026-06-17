"""Post-action verification and settle helpers."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable

from gui_agent.core.run.settle import settle_after_action, snapped_point
from gui_agent.core.schemas import SupervisorStep
from gui_agent.core.vision.target_verify import verify_target

VERIFY_TIMEOUT_S = 8


def submit_target_verify(
    *,
    action_decision: Any,
    executed: bool,
    sv_step: SupervisorStep,
    observation_png: bytes,
    pool: ThreadPoolExecutor,
) -> Future | None:
    """Submit post-action target verification if this turn executed a tap/click."""
    verify_point = snapped_point(action_decision) if executed else None
    if verify_point is None or not sv_step.instruction:
        return None
    return pool.submit(
        verify_target,
        observation_png,
        verify_point[0],
        verify_point[1],
        sv_step.instruction,
    )


def finalize_auto_continue_turn(
    *,
    turn,
    branch_settle_s: float | None,
    action_decision: Any,
    phone,
    observation_png: bytes,
    verify_future: Future | None,
    say: Callable[[str], None],
) -> None:
    """Attach settle timing and target verification results to a completed turn."""
    if branch_settle_s is not None:
        # Cached scrolling already settled while verifying displacement.
        turn.settle_s = branch_settle_s
    else:
        settle_action = action_decision.action if action_decision else None
        settle_action_type = settle_action.action_type if settle_action else None
        settle_focus_y = (
            settle_action.y
            if (settle_action and settle_action_type == "type" and settle_action.y is not None)
            else None
        )
        settle_center = (
            (settle_action.x, settle_action.y)
            if (
                settle_action
                and settle_action_type == "tap"
                and settle_action.x is not None
                and settle_action.y is not None
            )
            else None
        )
        turn.settle_s, turn.no_effect = settle_after_action(
            phone,
            observation_png,
            settle_action_type,
            settle_focus_y,
            center=settle_center,
        )

    if verify_future is None:
        return
    try:
        turn.target_verify = verify_future.result(timeout=VERIFY_TIMEOUT_S)
        tv = turn.target_verify
        if tv is not None and not tv.on_target:
            say(f"  [TargetVerify] off_target：标记落在「{tv.actual_element}」")
    except Exception as exc:
        say(f"  [TargetVerify] 校验失败（忽略）：{exc}")
