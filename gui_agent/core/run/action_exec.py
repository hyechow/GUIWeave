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
    if action_type not in ("drag", "scroll", "scroll_to_ref"):
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
    if action_type in ("drag", "scroll", "scroll_to_ref"):
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
    binding: TargetBinding | None = None
    supervisor_step: SupervisorStep | None = None


class ActionExecutor:
    """Resolve and dispatch exactly one action proposed by the Statement transition."""

    def run(
        self,
        *,
        sv_step: SupervisorStep,
        observation: Observation,
        action_policy,
        supervisor,
        executor,
        prep_future: Future,
        log_dir: Path,
        turn_no: int,
        flash: Callable[[Any], None],
        status: Callable[[int, str], None],
        say: Callable[[str], None],
        stop_requested: Callable[[], bool] | None = None,
        replan: Callable[[SupervisorStep, str], SupervisorStep] | None = None,
    ) -> ActionRunResult:
        result = ActionRunResult()
        intent = sv_step.action_intent
        if intent is None:
            return result

        def interrupted() -> bool:
            return bool(stop_requested and stop_requested())

        def rejected(reason: str) -> ActionRunResult:
            result.suppressed_reason = reason
            if replan is None:
                return result
            replacement = replan(sv_step, reason)
            if replacement.action_intent is None:
                result.supervisor_step = replacement
                return result
            retried = self.run(
                sv_step=replacement,
                observation=observation,
                action_policy=action_policy,
                supervisor=supervisor,
                executor=executor,
                prep_future=prep_future,
                log_dir=log_dir,
                turn_no=turn_no,
                flash=flash,
                status=status,
                say=say,
                stop_requested=stop_requested,
                replan=None,
            )
            retried.supervisor_step = replacement
            return retried

        say(f"动作指令: {intent.instruction}")
        if interrupted():
            say("  [Interrupt] 收到 ESC，跳过本轮动作执行")
            status(turn_no, "收到 ESC，跳过动作执行")
            return result
        if sv_step.preformed_action:
            say("使用预生成动作，跳过 Action Policy")
            result.action_decision = sv_step.preformed_action
        else:
            try:
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
            except Exception as exc:  # noqa: BLE001 - a grounding miss is statement feedback
                result.suppressed_reason = (
                    f"action policy could not ground the proposed intent: {exc}"
                )
                say(f"  [ActionPolicy] {result.suppressed_reason}")
                status(turn_no, "动作意图未能落成物理动作，交回 Statement 重决策")
                return rejected(result.suppressed_reason)

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
        action = action_decision.action
        result.action_role = effective_action_role(sv_step, action)
        if (
            action.action_type not in {"scroll", "drag", "scroll_to_ref"}
            and (intent.target_control or result.action_role == "write")
        ):
            binder = action_policy if callable(getattr(action_policy, "bind", None)) else None
            result.binding = bind_action_target(
                binder=binder,
                step=sv_step,
                observation=observation,
                action_decision=action_decision,
            )
            binding = result.binding
            if binding.status == "contradicted":
                result.suppressed_reason = binding.reason
                say(f"  [TargetBinding] contradicted: {binding.reason}")
                status(turn_no, "动作落点与声明目标冲突，未派发")
                return rejected(result.suppressed_reason)
            if result.action_role == "write" and binding.status == "unresolved":
                if intent.target_control:
                    # A target IS declared but structural identity could not confirm it
                    # (e.g. the adapter mis-extracted the control label). Dispatch the LLM's
                    # concrete visual choice but do not emit a bound mutation receipt.
                    say(f"  [TargetBinding] unresolved identity, failing open: {binding.reason}")
                else:
                    # No declared target at all → do not write blind.
                    result.suppressed_reason = (
                        f"target binding failed before dispatch: {binding.reason}"
                    )
                    say(f"  [TargetBinding] {result.suppressed_reason}")
                    status(turn_no, "目标绑定失败，未派发写动作")
                    return rejected(result.suppressed_reason)
            if binding.status == "bound":
                say(
                    "  [TargetBinding] "
                    f"{binding.source}:{binding.unit_id or 'control'}"
                )
        status(turn_no, f"[{action_label(action.action_type)}] {action.description}")

        flash(action)
        result.executed = executor.execute(
            action_decision,
            app_name=sv_step.app_name or "",
            png_bytes=observation.png_bytes,
            is_home_screen=sv_step.is_home_screen,
            target_control=intent.target_control,
        )
        result.action_role = effective_action_role(sv_step, action, observation)
        if result.executed and has_snapped_point(action_decision):
            flash(action)
        if result.executed:
            result.action_key = semantic_action_key(
                sv_step,
                result.action_decision.action,
                role=result.action_role,
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
        intent = sv_step.action_intent
        if intent is None:
            raise ValueError("action execution requires ActionIntent")
        instruction_for_action = intent.instruction
        started = time.perf_counter()
        token_before = get_llm_token_usage()
        action_decision = None
        target_group_id = ""
        grounding_kwargs = {
            "target_control": intent.target_control,
            "target_value": intent.target_value,
            "target_group_id": target_group_id,
            "action_family": intent.family,
        }
        if intent.target_ref:
            grounding_kwargs["target_ref"] = intent.target_ref
        native_resolver = getattr(action_policy, "resolve_native_action", None)
        if callable(native_resolver):
            action_decision = native_resolver(
                observation,
                **grounding_kwargs,
                instruction=instruction_for_action or "",
            )
        if action_decision is not None:
            reports = getattr(supervisor, "_context_reports", None)
            if isinstance(reports, list):
                reports.append({
                    "kind": "native_action",
                    "label": "execution.native_action",
                    "statement_id": sv_step.statement_id,
                    "target_control": intent.target_control,
                    "target_value": intent.target_value,
                    "target_group_id": target_group_id,
                    "action_family": intent.family,
                    "primitive": action_decision.action.action_type,
                    "fallback": False,
                })
            say("原生控件已唯一解析，跳过视觉 Action Policy")
        else:
            evidence_context = ""
            evidence_builder = getattr(action_policy, "action_evidence_context", None)
            if callable(evidence_builder):
                evidence_context = evidence_builder(
                    observation,
                    **grounding_kwargs,
                )
            action_decision = action_policy.decide(
                observation,
                instruction_for_action,
                direction=intent.direction,
                drag_column=intent.drag_column,
                drag_steps=intent.drag_steps,
                action_family=intent.family,
                target_control=intent.target_control,
                target_value=intent.target_value,
                expected_result=intent.expected_result,
                evidence_context=evidence_context,
                context_reports=getattr(supervisor, "_context_reports", None),
            )
            grounder = getattr(action_policy, "ground_rendered_action", None)
            if callable(grounder):
                ungrounded = action_decision
                action_decision = grounder(
                    action_decision,
                    observation,
                    **grounding_kwargs,
                )
                if action_decision is not ungrounded:
                    reports = getattr(supervisor, "_context_reports", None)
                    if isinstance(reports, list):
                        reports.append({
                            "kind": "action_grounding",
                            "label": "execution.action_grounding",
                            "statement_id": sv_step.statement_id,
                            "target_control": intent.target_control,
                            "target_group_id": target_group_id,
                            "primitive": action_decision.action.action_type,
                        })
        if hasattr(supervisor, "_timings"):
            supervisor._timings["action_policy"] = (
                supervisor._timings.get("action_policy", 0.0)
                + time.perf_counter()
                - started
            )
            if "action_policy" not in supervisor._timings_order:
                supervisor._timings_order.append("action_policy")
        if hasattr(supervisor, "_token_usage"):
            token_after = get_llm_token_usage()
            prior = supervisor._token_usage.get("action_policy", {})
            supervisor._token_usage["action_policy"] = {
                "input": int(prior.get("input", 0)) + token_after[0] - token_before[0],
                "output": int(prior.get("output", 0)) + token_after[1] - token_before[1],
            }
        print_decision(
            action_decision,
            observation.png_bytes,
            log_dir / f"structured_output_result_turn_{turn_no}.png",
        )
        return action_decision

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
    intent = sv_step.action_intent
    if verify_point is None or intent is None:
        return None
    return pool.submit(
        verify_target,
        observation_png,
        verify_point[0],
        verify_point[1],
        intent.instruction,
    )


def finalize_auto_continue_turn(
    *,
    turn,
    action_decision: Any,
    platform,
    observation_png: bytes,
    verify_future: Future | None,
    say: Callable[[str], None],
) -> None:
    """Attach settle timing and target verification results to a completed turn."""
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
