"""Action decision and execution helpers for the agent loop."""

from __future__ import annotations

import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from llm.structured import get_llm_token_usage

from gui_agent.core.schemas import (
    ActionDecision,
    AtomicRole,
    Observation,
    SupervisorStep,
    TargetBinding,
    action_label,
)
from gui_agent.core.run.action_signals import (
    effective_action_role,
    record_response,
    record_settle,
    record_target_verification,
    semantic_action_key,
)
from gui_agent.core.run.target_binding import bind_action_target
from gui_agent.core.vision.frame_analysis import STABLE_MEAN_THR, frame_changed, frame_diff
from gui_agent.core.vision.target_verify import verify_target
from gui_agent.core.vision.visualize import print_decision

VERIFY_TIMEOUT_S = 8
SETTLE_FIRST_S = 1.0
SETTLE_UNIT_S = 0.5
SETTLE_MAX_UNITS = 6
SETTLE_GESTURE_FIRST_S = 0.3


def settle_after_action(
    platform: object,
    pre_frame: bytes | None,
    action_type: str | None = None,
    focus_y: float | None = None,
    center: tuple[float, float] | None = None,
) -> tuple[float, bool]:
    """Wait until the screen changed and settled, or hit the cap."""
    if action_type not in ("drag", "scroll"):
        cdp_settle = getattr(platform, "wait_settled", None)
        if cdp_settle is not None:
            try:
                elapsed, no_effect = cdp_settle(action_type)
                if no_effect and pre_frame is not None:
                    current = platform.screenshot()
                    if frame_changed(pre_frame, current, focus_y, center=center):
                        print("  [Settle] CDP 未观察到反馈，但视觉帧已变化")
                        no_effect = False
                return elapsed, no_effect
            except Exception as exc:
                print(f"  [Settle] CDP settle 异常，回退视觉: {exc}")
    started = time.perf_counter()
    if action_type in ("drag", "scroll"):
        prev: bytes | None = None
        for i in range(1, SETTLE_MAX_UNITS + 1):
            time.sleep(SETTLE_GESTURE_FIRST_S if i == 1 else SETTLE_UNIT_S)
            try:
                cur = platform.screenshot()
            except Exception:
                elapsed = time.perf_counter() - started
                print(f"  [Settle] {elapsed:.1f}s ({i} 轮，截图异常提前返回)")
                return elapsed, False
            if prev is not None and frame_diff(prev, cur) < STABLE_MEAN_THR:
                elapsed = time.perf_counter() - started
                print(f"  [Settle] {elapsed:.1f}s ({i} 轮，停稳: {action_type})")
                return elapsed, False
            prev = cur
        elapsed = time.perf_counter() - started
        print(f"  [Settle] {elapsed:.1f}s ({SETTLE_MAX_UNITS} 轮，达上限: {action_type})")
        return elapsed, False
    if pre_frame is None:
        time.sleep(SETTLE_FIRST_S)
        elapsed = time.perf_counter() - started
        print(f"  [Settle] {elapsed:.1f}s (无动作前帧)")
        return elapsed, False
    prev: bytes | None = None
    ever_changed = False
    pop_tab = getattr(platform, "pop_tab_switched", None)
    for i in range(1, SETTLE_MAX_UNITS + 1):
        time.sleep(SETTLE_FIRST_S if i == 1 else SETTLE_UNIT_S)
        try:
            cur = platform.screenshot()
        except Exception:
            elapsed = time.perf_counter() - started
            print(f"  [Settle] {elapsed:.1f}s ({i} 轮，截图异常提前返回)")
            return elapsed, False
        tab_just_switched = bool(pop_tab and pop_tab())
        if tab_just_switched:
            ever_changed = True
            print(f"  [Settle] {time.perf_counter() - started:.1f}s ({i} 轮，tab切换→有效果)")
        changed = frame_changed(pre_frame, cur, focus_y, center=center)
        ever_changed = ever_changed or changed
        stable = prev is not None and frame_diff(prev, cur, focus_y) < STABLE_MEAN_THR
        if (changed or tab_just_switched) and stable:
            elapsed = time.perf_counter() - started
            print(f"  [Settle] {elapsed:.1f}s ({i} 轮，变过且停稳)")
            return elapsed, False
        prev = cur
    elapsed = time.perf_counter() - started
    no_effect = not ever_changed
    tag = "达上限·零效果" if no_effect else "达上限"
    print(f"  [Settle] {elapsed:.1f}s ({SETTLE_MAX_UNITS} 轮，{tag})")
    return elapsed, no_effect


def snapped_point(action_decision: ActionDecision | None) -> tuple[float, float] | None:
    """Return the actual tap location, snapped if DOM/vision snapping fired."""
    if action_decision is None:
        return None
    action = action_decision.action
    if action is None:
        return None
    if action.action_type not in ("tap", "click") or action.x is None or action.y is None:
        return None
    snap = action.snap
    if snap and snap.get("snapped"):
        sx, sy = snap["snapped"]
        return float(sx), float(sy)
    return float(action.x), float(action.y)


def has_snapped_point(action_decision: ActionDecision | None) -> bool:
    """Return true when the executor recorded a corrected action point."""
    if action_decision is None:
        return False
    snap = getattr(action_decision.action, "snap", None)
    if not isinstance(snap, dict):
        return False
    snapped = snap.get("snapped")
    return isinstance(snapped, (list, tuple)) and len(snapped) >= 2


@dataclass
class ActionRunResult:
    action_decision: Any = None
    executed: bool = False
    action_role: AtomicRole = "prepare"
    action_key: str = ""
    suppressed_reason: str = ""
    probe_failed: bool = False
    branch_settle_s: float | None = None
    binding: TargetBinding | None = None


class ActionExecutionState:
    """Own scroll-probe caches and run the action-policy/execute branch."""

    def __init__(self) -> None:
        self.scroll_profiles: dict[str, Any] = {}
        self.scroll_probe_failures: dict[str, str] = {}

    @staticmethod
    def _scroll_profile_key(step: SupervisorStep) -> str:
        scope = step.execution_scope or step.statement_id or "_global"
        target = (step.target_control or "_viewport").strip().lower()
        direction = (step.direction or "down").strip().lower()
        return f"{scope}|{target}|{direction}"

    def run(
        self,
        *,
        sv_step: SupervisorStep,
        observation: Observation,
        action_policy,
        supervisor,
        history: list[Any] | None = None,
        executor,
        bundle,
        platform,
        prep_future: Future,
        log_dir: Path,
        turn_no: int,
        flash: Callable[[Any], None],
        status: Callable[[int, str], None],
        say: Callable[[str], None],
        stop_requested: Callable[[], bool] | None = None,
    ) -> ActionRunResult:
        result = ActionRunResult()
        if not sv_step.should_act:
            return result

        def interrupted() -> bool:
            return bool(stop_requested and stop_requested())

        say(f"动作指令: {sv_step.instruction}")
        if interrupted():
            say("  [Interrupt] 收到 ESC，跳过本轮动作执行")
            status(turn_no, "收到 ESC，跳过动作执行")
            return result
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

        if interrupted():
            say("  [Interrupt] 收到 ESC，跳过本轮动作执行")
            status(turn_no, "收到 ESC，跳过动作执行")
            return result

        # Ensure YOLO/OCR prep finished before any execute/snap (covers both the
        # preformed-action and action-policy paths). Started after screenshot,
        # overlapped the decide, and is normally done already.
        prep_future.result()
        if interrupted():
            say("  [Interrupt] 收到 ESC，跳过本轮动作执行")
            status(turn_no, "收到 ESC，跳过动作执行")
            return result
        action_decision = result.action_decision
        if action_decision.not_found_reason:
            say(f"  [NotFound] {action_decision.not_found_reason}")
            status(turn_no, "未找到目标元素")
            return result

        action = action_decision.action
        if action is None:
            say("  [NoAction] Action Policy 未返回可执行动作")
            status(turn_no, "未产生可执行动作")
            return result
        result.action_role = effective_action_role(sv_step, action)
        if result.action_role == "write":
            authorization = sv_step.mutation_authorization
            if sv_step.requires_mutation_authorization and authorization is None:
                result.suppressed_reason = (
                    "mutation write has no system-resolved subject authorization"
                )
                say(f"  [Mutation] {result.suppressed_reason}")
                status(turn_no, "mutation subject 未解析，未派发写动作")
                return result
            binder = action_policy if callable(getattr(action_policy, "bind", None)) else None
            result.binding = bind_action_target(
                binder=binder,
                step=sv_step,
                observation=observation,
                action_decision=action_decision,
            )
            if result.binding.status != "bound":
                result.suppressed_reason = (
                    "target binding failed before dispatch: "
                    f"{result.binding.status}: {result.binding.reason}"
                )
                say(f"  [TargetBinding] {result.suppressed_reason}")
                status(turn_no, "目标绑定失败，未派发写动作")
                return result
            if authorization is not None:
                binding_matches = (
                    result.binding.source == authorization.source
                    and result.binding.unit_id == authorization.subject_ref
                )
                if not binding_matches:
                    result.suppressed_reason = (
                        "mutation action point does not match its authorized subject: "
                        f"expected {authorization.source}:{authorization.subject_ref}, got "
                        f"{result.binding.source}:{result.binding.unit_id or 'control'}"
                    )
                    say(f"  [Mutation] {result.suppressed_reason}")
                    status(turn_no, "mutation 写入目标与授权 subject 不一致")
                    return result
            say(
                "  [TargetBinding] "
                f"{result.binding.source}:{result.binding.unit_id or 'control'}"
            )
        status(turn_no, f"[{action_label(action.action_type)}] {action.description}")

        profile_key = self._scroll_profile_key(sv_step)
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
                platform=platform,
                flash=flash,
                say=say,
            )
            action_decision = result.action_decision
            action = action_decision.action
            if action is None:
                say("  [NoAction] grounding 后未返回可执行动作")
                status(turn_no, "未产生可执行动作")
                return result

        if should_probe_scroll and not result.executed and not result.probe_failed:
            self._probe_scroll(
                result=result,
                action=action,
                action_decision=action_decision,
                profile_key=profile_key,
                observation=observation,
                executor=executor,
                bundle=bundle,
                platform=platform,
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
            if result.executed and has_snapped_point(action_decision):
                flash(action)
        if result.executed and result.action_decision.action is not None:
            result.action_key = semantic_action_key(
                sv_step, result.action_decision.action
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
        profile_key = self._scroll_profile_key(sv_step)
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
        action_decision = None
        authorization = sv_step.mutation_authorization
        target_group_id = (
            authorization.subject_ref
            if authorization is not None and authorization.source == "structural"
            else ""
        )
        native_resolver = getattr(action_policy, "resolve_native_action", None)
        if callable(native_resolver):
            action_decision = native_resolver(
                observation,
                target_control=sv_step.target_control,
                target_value=sv_step.target_value,
                target_group_id=target_group_id,
                action_family=sv_step.action_family,
                instruction=instruction_for_action or "",
            )
        if action_decision is not None:
            reports = getattr(supervisor, "_context_reports", None)
            if isinstance(reports, list):
                reports.append({
                    "kind": "native_action",
                    "label": "execution.native_action",
                    "statement_id": sv_step.statement_id,
                    "target_control": sv_step.target_control,
                    "target_value": sv_step.target_value,
                    "target_group_id": target_group_id,
                    "action_family": sv_step.action_family,
                    "primitive": action_decision.action.action_type if action_decision.action else "none",
                    "fallback": False,
                })
            say("原生控件已唯一解析，跳过视觉 Action Policy")
        else:
            evidence_context = ""
            evidence_builder = getattr(action_policy, "action_evidence_context", None)
            if callable(evidence_builder):
                evidence_context = evidence_builder(
                    observation,
                    target_control=sv_step.target_control,
                    target_value=sv_step.target_value,
                    target_group_id=target_group_id,
                    action_family=sv_step.action_family,
                )
            action_decision = action_policy.decide(
                observation,
                instruction_for_action,
                direction=sv_step.direction,
                drag_column=sv_step.drag_column,
                drag_steps=sv_step.drag_steps,
                evidence_context=evidence_context,
                context_reports=getattr(supervisor, "_context_reports", None),
            )
            grounder = getattr(action_policy, "ground_rendered_action", None)
            if callable(grounder):
                ungrounded = action_decision
                action_decision = grounder(
                    action_decision,
                    observation,
                    target_control=sv_step.target_control,
                    target_value=sv_step.target_value,
                    target_group_id=target_group_id,
                    action_family=sv_step.action_family,
                )
                if action_decision is not ungrounded:
                    reports = getattr(supervisor, "_context_reports", None)
                    if isinstance(reports, list):
                        reports.append({
                            "kind": "action_grounding",
                            "label": "execution.action_grounding",
                            "statement_id": sv_step.statement_id,
                            "target_control": sv_step.target_control,
                            "target_group_id": target_group_id,
                            "primitive": action_decision.action.action_type if action_decision.action else "none",
                        })
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
        platform,
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
            platform,
            observation.png_bytes,
            cached.action_type,
        )
        after_png = platform.screenshot()
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
        platform,
        log_dir: Path,
        turn_no: int,
        flash: Callable[[Any], None],
        say: Callable[[str], None],
    ) -> None:
        flash(action)
        probe = bundle.make_scroll_probe(platform, executor, log_dir)
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
    platform,
    observation_png: bytes,
    verify_future: Future | None,
    say: Callable[[str], None],
) -> None:
    """Attach settle timing and target verification results to a completed turn."""
    if branch_settle_s is not None:
        # Cached scrolling already settled while verifying displacement.
        record_settle(turn, elapsed_s=branch_settle_s, no_effect=False)
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
        settle_s, no_effect = settle_after_action(
            platform,
            observation_png,
            settle_action_type,
            settle_focus_y,
            center=settle_center,
        )
        record_settle(turn, elapsed_s=settle_s, no_effect=no_effect)

    if verify_future is None:
        record_response(
            turn,
            observed=not turn.no_effect,
            channels=("visual",) if not turn.no_effect else (),
        )
        return
    try:
        tv = verify_future.result(timeout=VERIFY_TIMEOUT_S)
        record_target_verification(turn, tv)
        if tv is not None and not tv.on_target:
            say(f"  [TargetVerify] off_target：标记落在「{tv.actual_element}」")
    except Exception as exc:
        say(f"  [TargetVerify] 校验失败（忽略）：{exc}")
    record_response(
        turn,
        observed=not turn.no_effect,
        channels=("visual",) if not turn.no_effect else (),
    )
