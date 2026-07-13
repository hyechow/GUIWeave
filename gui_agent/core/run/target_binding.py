"""One-shot target binding for concrete write actions."""

from __future__ import annotations

from gui_agent.core.schemas import (
    BaseActionDecision,
    Observation,
    SupervisorStep,
    TargetBinding,
)


def _visual_binding(
    step: SupervisorStep,
    action_decision: BaseActionDecision,
) -> TargetBinding:
    action = action_decision.action
    x = getattr(action, "x", None)
    y = getattr(action, "y", None)
    if not step.target_control or not step.target_value:
        return TargetBinding(
            status="unresolved",
            reason="visual binding requires a declared control and value",
        )
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        return TargetBinding(
            status="unresolved",
            reason="visual action has no concrete target point",
        )
    return TargetBinding(
        status="bound",
        source="visual",
        unit_id=step.target_group_id,
        reason="bound to the concrete visual action point",
    )


def bind_action_target(
    *,
    binder: object | None,
    step: SupervisorStep,
    observation: Observation,
    action_decision: BaseActionDecision,
) -> TargetBinding:
    resolve = getattr(binder, "bind", None)
    if callable(resolve):
        binding = resolve(step, observation, action_decision)
        if binding is not None:
            return binding
    return _visual_binding(step, action_decision)


__all__ = ["bind_action_target"]
