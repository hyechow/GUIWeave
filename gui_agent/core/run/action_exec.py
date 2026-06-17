"""Action decision and execution helpers for the agent loop."""

from __future__ import annotations

import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from llm.structured import get_llm_token_usage

from gui_agent.core.run.settle import settle_after_action, snapped_point
from gui_agent.core.schemas import ActionDecision, Observation, SupervisorStep, action_label
from gui_agent.core.vision.target_verify import verify_target
from gui_agent.core.vision.visualize import print_decision

VERIFY_TIMEOUT_S = 8


@dataclass
class ActionRunResult:
    action_decision: Any = None
    executed: bool = False
    probe_failed: bool = False
    branch_settle_s: float | None = None


class ActionExecutionState:
    """Own scroll-probe caches and run the action-policy/execute branch."""

    def __init__(self) -> None:
        self.scroll_profiles: dict[str, Any] = {}
        self.scroll_probe_failures: dict[str, str] = {}

    def run(
        self,
        *,
        sv_step: SupervisorStep,
        observation: Observation,
        action_policy,
        supervisor,
        executor,
        bundle,
        phone,
        prep_future: Future,
        log_dir: Path,
        turn_no: int,
        flash: Callable[[Any], None],
        status: Callable[[int, str], None],
        say: Callable[[str], None],
    ) -> ActionRunResult:
        result = ActionRunResult()
        if not sv_step.should_act:
            return result

        say(f"动作指令: {sv_step.instruction}")
        if sv_step.preformed_action:
            say("使用预生成动作，跳过 Action Policy")
            result.action_decision = sv_step.preformed_action
        else:
            result.action_decision = self._decide_action(
                sv_step=sv_step,
                observation=observation,
                action_policy=action_policy,
                supervisor=supervisor,
                log_dir=log_dir,
                turn_no=turn_no,
                status=status,
                say=say,
            )

        # Ensure YOLO/OCR prep finished before any execute/snap (covers both the
        # preformed-action and action-policy paths). Started after screenshot,
        # overlapped the decide, and is normally done already.
        prep_future.result()
        action_decision = result.action_decision
        if action_decision.not_found_reason:
            say(f"  [NotFound] {action_decision.not_found_reason}")
            status(turn_no, "未找到目标元素")
            return result

        action = action_decision.action
        if action_decision.action:
            status(turn_no, f"[{action_label(action.action_type)}] {action.description}")

        profile_key = sv_step.milestone_id or "_global"
        should_probe_scroll = (
            action.action_type == "scroll"
            and sv_step.completion_strategy == "scroll_until_boundary"
        )
        if should_probe_scroll and profile_key in self.scroll_profiles:
            self._try_cached_scroll(
                result=result,
                action=action,
                action_decision=action_decision,
                profile_key=profile_key,
                sv_step=sv_step,
                observation=observation,
                executor=executor,
                bundle=bundle,
                phone=phone,
                flash=flash,
                say=say,
            )
            action_decision = result.action_decision
            action = action_decision.action

        if should_probe_scroll and not result.executed and not result.probe_failed:
            self._probe_scroll(
                result=result,
                action=action,
                action_decision=action_decision,
                profile_key=profile_key,
                observation=observation,
                executor=executor,
                bundle=bundle,
                phone=phone,
                log_dir=log_dir,
                turn_no=turn_no,
                flash=flash,
                say=say,
            )
        elif not should_probe_scroll:
            flash(action)
            result.executed = executor.execute(
                action_decision,
                app_name=sv_step.app_name or "",
                png_bytes=observation.png_bytes,
                is_home_screen=sv_step.is_home_screen,
            )
        return result

    def _decide_action(
        self,
        *,
        sv_step: SupervisorStep,
        observation: Observation,
        action_policy,
        supervisor,
        log_dir: Path,
        turn_no: int,
        status: Callable[[int, str], None],
        say: Callable[[str], None],
    ):
        status(turn_no, "动作决策中…")
        say("动作决策中...")
        instruction_for_action = sv_step.instruction
        profile_key = sv_step.milestone_id or "_global"
        if (
            sv_step.completion_strategy == "scroll_until_boundary"
            and profile_key in self.scroll_probe_failures
        ):
            instruction_for_action = (
                f"{instruction_for_action}\n\n"
                "⚠️ 滚动探测反馈："
                f"{self.scroll_probe_failures[profile_key]}。"
                "请避免重复这些无效滚动落点/幅度，选择当前屏幕上更可能作用于主内容的滚动方式。"
            )
        started = time.perf_counter()
        token_before = get_llm_token_usage()
        action_decision = action_policy.decide(
            observation,
            instruction_for_action,
            direction=sv_step.direction,
            drag_column=sv_step.drag_column,
            drag_steps=sv_step.drag_steps,
        )
        if hasattr(supervisor, "_timings"):
            supervisor._timings["action_policy"] = time.perf_counter() - started
            supervisor._timings_order.append("action_policy")
        if hasattr(supervisor, "_token_usage"):
            token_after = get_llm_token_usage()
            supervisor._token_usage["action_policy"] = {
                "input": token_after[0] - token_before[0],
                "output": token_after[1] - token_before[1],
            }
        print_decision(
            action_decision,
            observation.png_bytes,
            log_dir / f"structured_output_result_turn_{turn_no}.png",
        )
        return action_decision

    def _try_cached_scroll(
        self,
        *,
        result: ActionRunResult,
        action,
        action_decision,
        profile_key: str,
        sv_step: SupervisorStep,
        observation: Observation,
        executor,
        bundle,
        phone,
        flash: Callable[[Any], None],
        say: Callable[[str], None],
    ) -> None:
        profile = self.scroll_profiles[profile_key]
        cached = bundle.apply_scroll_profile(action, profile)
        say(
            "  [ScrollProbe] 使用缓存滚动点: "
            f"method={profile.method}, x={profile.x:.0f}, y={profile.y:.0f}, "
            f"ticks={profile.ticks}, delta={profile.delta_px}"
        )
        flash(cached)
        if cached.action_type == "scroll":
            print(f"\n动作: [{cached.action_type}] {cached.description}")
            executor.execute_scroll(cached, ticks=profile.ticks, delta_px=profile.delta_px)
        else:
            executor.execute(ActionDecision(action=cached), app_name=sv_step.app_name or "")

        result.branch_settle_s, _ = settle_after_action(
            phone,
            observation.png_bytes,
            cached.action_type,
        )
        after_png = phone.screenshot()
        cshift, _ = bundle.robust_shift(
            bundle.gray_u8(observation.png_bytes),
            bundle.gray_u8(after_png),
        )
        if cshift != 0:
            action_decision = action_decision.model_copy(update={"action": cached})
            result.action_decision = action_decision
            result.executed = True
            return

        say("  [ScrollProbe] 缓存滚动点 settle 后仍 0 位移 → 废弃缓存，重新探测")
        self.scroll_profiles.pop(profile_key, None)
        result.branch_settle_s = None

    def _probe_scroll(
        self,
        *,
        result: ActionRunResult,
        action,
        action_decision,
        profile_key: str,
        observation: Observation,
        executor,
        bundle,
        phone,
        log_dir: Path,
        turn_no: int,
        flash: Callable[[Any], None],
        say: Callable[[str], None],
    ) -> None:
        flash(action)
        probe = bundle.make_scroll_probe(phone, executor, log_dir)
        probe_result = probe.probe(observation.png_bytes, action, turn_no=turn_no)
        if probe_result.success and probe_result.profile:
            self.scroll_profiles[profile_key] = probe_result.profile
            self.scroll_probe_failures.pop(profile_key, None)
            action = bundle.apply_scroll_profile(action, probe_result.profile)
            result.action_decision = action_decision.model_copy(update={"action": action})
            result.executed = True
            return

        result.probe_failed = True
        self.scroll_probe_failures[profile_key] = probe_result.reason
        say(
            "  [ScrollProbe] 未找到可靠滚动点，停止本轮动作: "
            f"{probe_result.reason}"
        )


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
