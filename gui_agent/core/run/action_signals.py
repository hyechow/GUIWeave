"""Create and update persisted action-delivery facts.

Sensors supply raw dispatch, target, and response facts.  This module is their only writer into
``ActionSignal``; it does not infer business effect, persistence, or control flow.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from gui_agent.core.schemas import (
    ActionSignal,
    AtomicRole,
    MutationReceipt,
    Observation,
    PolicyTurn,
    SupervisorStep,
    TargetBinding,
)

_COMMIT_ACTIONS = frozenset({"tap", "click", "press_enter"})
_ITERATIVE_ACTIONS = frozenset({"scroll", "drag", "scroll_to_ref"})
_WRITE_ACTIONS = frozenset({"type", "clear_text", "select_option"})
_POSITION_BIN = 50


def has_uncommitted_write(
    history: Iterable[PolicyTurn],
    *,
    commit_intent: bool = False,
) -> bool:
    """Whether the latest valid write awaits commit, optionally after commit intent."""
    for turn in reversed(list(history)):
        signal = turn.action_signal
        if (
            signal is None
            or signal.execution != "dispatched"
            or signal.target == "off_target"
            or signal.role not in {"write", "commit"}
        ):
            continue
        if signal.role == "commit":
            return False
        intent = turn.supervisor.action_intent
        return not commit_intent or (intent is not None and intent.role == "commit")
    return False


def latest_action(
    history: Iterable[PolicyTurn],
    statement_id: str,
    *,
    scope: str = "",
    role: str = "",
) -> PolicyTurn | None:
    """Return the newest matching dispatch from immutable action-signal history."""
    return next(
        (
            turn
            for turn in reversed(list(history))
            if turn.supervisor is not None
            and turn.supervisor.statement_id == statement_id
            and turn.action_signal is not None
            and turn.action_signal.execution == "dispatched"
            and (not role or turn.action_signal.role == role)
            and (role != "write" or turn.action_signal.target != "off_target")
            and (
                not scope
                or getattr(turn.supervisor, "execution_scope", "") in {"", scope}
            )
        ),
        None,
    )


def normalize_action_text(value: str) -> str:
    """Collapse cosmetic wording differences in action-history identities."""
    text = (value or "").lower()
    text = re.sub(r"[\s，。、；;：:！!？?（）()「」『』\[\]【】\"'`’‘“”]+", "", text)
    return text[:80]


def action_signature(action: Any) -> str:
    """Return a reword-proof identity for one concrete primitive and target."""
    action_type = getattr(action, "action_type", "") or "?"
    snap = getattr(action, "snap", None) or {}
    info = snap.get("info") or ""
    tag = info.split(" ", 1)[0].lower() if info else ""
    point = snap.get("snapped") or [
        getattr(action, "x", None), getattr(action, "y", None)
    ]
    if point and point[0] is not None and len(point) > 1 and point[1] is not None:
        position = f"{int(point[0] // _POSITION_BIN)},{int(point[1] // _POSITION_BIN)}"
    else:
        position = "-"
    target = f"{tag}@{position}" if tag else f"@{position}"
    if action_type == "scroll_to_ref":
        return f"{action_type}|ref:{getattr(action, 'target_ref', '') or ''}"
    if action_type in _ITERATIVE_ACTIONS:
        return f"{action_type}|{getattr(action, 'direction', '') or ''}|{target}"
    text = normalize_action_text(getattr(action, "text", "") or "")
    return f"{action_type}|{target}|{text}"


def effective_action_role(
    step: SupervisorStep,
    action: Any,
    observation: Observation | None = None,
) -> AtomicRole:
    """Resolve the lifecycle role from the concrete primitive."""
    action_type = str(getattr(action, "action_type", "") or "").lower()
    if action_type in _ITERATIVE_ACTIONS:
        return "iterate"
    if action_type in _WRITE_ACTIONS:
        return "write"
    if action_type not in _COMMIT_ACTIONS:
        return "prepare"
    intent = step.action_intent
    role = intent.role if intent is not None else "prepare"
    if observation is None:
        return role
    snap = getattr(action, "snap", None) or {}
    point = snap.get("snapped") or (
        getattr(action, "x", None),
        getattr(action, "y", None),
    )
    commit_rects = [
        control.get("rect")
        for control in (
            observation.form_control_state
            or observation.form_controls
            or []
        )
        if control.get("form_action") == "commit"
        and isinstance(control.get("rect"), dict)
    ]
    if not commit_rects or None in point:
        return role
    x, y = point
    if any(
        rect["x"] <= x <= rect["x"] + rect["w"]
        and rect["y"] <= y <= rect["y"] + rect["h"]
        for rect in commit_rects
        if all(key in rect for key in ("x", "y", "w", "h"))
    ):
        return "commit"
    return "write" if role == "commit" else role


def semantic_action_key(
    step: SupervisorStep,
    action: Any,
    *,
    role: AtomicRole | None = None,
) -> str:
    """Return one stable identity for the concrete action in its execution scope."""
    role = role or effective_action_role(step, action)
    prefix = f"{step.execution_scope or ''}|{step.statement_id or ''}|{role}"
    if role == "commit":
        return prefix
    return f"{prefix}|{action_signature(action)}"


def build_action_signal(
    step: SupervisorStep,
    action_decision,
    *,
    role: AtomicRole,
    action_key: str,
    surface_id: str,
    executed: bool,
    suppressed_reason: str,
    binding: TargetBinding | None,
) -> ActionSignal | None:
    """Record facts known when one concrete primitive reaches the dispatch boundary."""
    action = action_decision.action if action_decision else None
    intent = step.action_intent
    if action_decision is None and not suppressed_reason:
        return None
    execution = (
        "dispatched"
        if executed
        else "dispatch_failed"
        if action_decision is not None and not suppressed_reason
        else "not_attempted"
    )
    bound_subject = (
        binding.unit_id
        if binding is not None and binding.status == "bound"
        else ""
    )
    receipt = (
        MutationReceipt(
            statement_id=step.statement_id or "",
            subject_ref=bound_subject,
            field=intent.target_control,
            intended_value=intent.target_value,
            source=binding.source or "visual",
        )
        if role == "write"
        and execution == "dispatched"
        and bound_subject
        and binding is not None
        and binding.source is not None
        and intent is not None
        and intent.target_control
        and intent.target_value
        else None
    )
    return ActionSignal(
        action_key=action_key,
        role=role,
        surface_id=surface_id,
        target_control=intent.target_control if intent is not None else "",
        target_value=(
            (intent.target_value or str(getattr(action, "text", "") or ""))
            if role == "write" and intent is not None
            else ""
        ),
        mutation_receipt=receipt,
        binding=binding,
        execution=execution,
        target=(
            "off_target"
            if binding is not None and binding.status == "contradicted"
            else "unknown"
        ),
        suppressed_reason=suppressed_reason,
        evidence=([suppressed_reason] if suppressed_reason else []),
    )


def record_response(
    turn: PolicyTurn,
    *,
    observed: bool,
    channels: Iterable[str] = (),
) -> None:
    """Append one sensor's response observation without replacing other channels."""
    signal = getattr(turn, "action_signal", None)
    if signal is None:
        return
    if observed:
        signal.response = "observed"
        for channel in channels:
            if channel and channel not in signal.response_channels:
                signal.response_channels.append(channel)
    elif signal.response == "unknown":
        signal.response = "none_observed"


def record_latest_structured_response(
    history: list[PolicyTurn],
    statement_id: str,
    *,
    url_changed: bool,
    dom_changed: bool,
) -> None:
    """Attach fresh URL/DOM deltas to the dispatch that preceded the observation."""
    turn = latest_action(history, statement_id)
    if turn is None:
        return
    channels = tuple(
        channel
        for channel, changed in (("url", url_changed), ("dom", dom_changed))
        if changed
    )
    if channels:
        record_response(turn, observed=True, channels=channels)
    elif turn.no_effect:
        record_response(turn, observed=False)


def record_target(turn: PolicyTurn, *, on_target: bool) -> None:
    """Attach the post-dispatch target-verifier result."""
    signal = getattr(turn, "action_signal", None)
    if signal is not None:
        signal.target = "on_target" if on_target else "off_target"


def record_settle(
    turn: PolicyTurn,
    *,
    elapsed_s: float,
    no_effect: bool,
) -> None:
    """Finalize settle facts through the journal turn's sole delivery writer."""
    turn.settle_s = elapsed_s
    turn.no_effect = no_effect


def record_target_verification(turn: PolicyTurn, verification: Any) -> None:
    """Attach the verifier payload and its normalized target fact together."""
    turn.target_verify = verification
    if verification is not None:
        record_target(turn, on_target=bool(verification.on_target))


__all__ = [
    "action_signature",
    "build_action_signal",
    "effective_action_role",
    "has_uncommitted_write",
    "latest_action",
    "normalize_action_text",
    "record_latest_structured_response",
    "record_response",
    "record_settle",
    "record_target",
    "record_target_verification",
    "semantic_action_key",
]
