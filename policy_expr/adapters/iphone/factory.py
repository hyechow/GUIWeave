"""iPhone adapter factory: the one place iPhone construction is wired together.

Builds a :class:`policy_expr.core.factory.PlatformBundle` whose callables
construct the iPhone-mirroring session, executor, perception, action policy,
supervisor and HUD. This is the only module allowed to know how all the iPhone
pieces fit together; core orchestration receives the neutral bundle and never
imports these classes directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from policy_expr.core.factory import PlatformBundle

from policy_expr.adapters.iphone.executor import ActionExecutor
from policy_expr.adapters.iphone.perception import LivePerception, LivePhoneSession
from policy_expr.adapters.iphone.policies.structured_output import StructuredOutputPolicy
from policy_expr.adapters.iphone.supervisor.simple import SimpleSupervisorPolicy
from policy_expr.supervisor.milestone.policy import MilestoneSupervisorPolicy

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from policy_expr.core.contracts import ActionPolicy, SupervisorPolicy


# Registries (mirrors of the legacy dicts in runner.py; runner keeps its own copy
# until the Step-4 flip routes selection through the bundle).
_POLICIES: dict[str, type] = {
    StructuredOutputPolicy.name: StructuredOutputPolicy,
}
_SUPERVISORS: dict[str, type] = {
    SimpleSupervisorPolicy.name: SimpleSupervisorPolicy,
    MilestoneSupervisorPolicy.name: MilestoneSupervisorPolicy,
}


def _build_action_policy(name: str) -> "ActionPolicy":
    try:
        return _POLICIES[name]()
    except KeyError as exc:
        choices = ", ".join(sorted(_POLICIES))
        raise ValueError(f"未知策略 {name!r}，可选：{choices}") from exc


def _build_supervisor(name: str) -> "SupervisorPolicy":
    try:
        return _SUPERVISORS[name]()
    except KeyError as exc:
        choices = ", ".join(sorted(_SUPERVISORS))
        raise ValueError(f"未知监督者 {name!r}，可选：{choices}") from exc


def _make_hud() -> "AbstractContextManager":
    from policy_expr.adapters.iphone.hud import AgentHUD

    return AgentHUD()


def build_iphone_bundle(*, backend: Optional[str] = None, **_ignored: object) -> PlatformBundle:
    """Construct the iPhone PlatformBundle. ``backend`` is the daemon/mirroir knob
    (passed through to LivePhoneSession; None lets it fall back to AGENT_MODE)."""
    return PlatformBundle(
        platform="iphone",
        open_session=lambda: LivePhoneSession(backend=backend),
        make_executor=lambda phone: ActionExecutor(phone),
        make_perception=lambda phone, png_path: LivePerception(phone, png_path),
        make_action_policy=_build_action_policy,
        make_supervisor=_build_supervisor,
        make_status_reporter=lambda enabled: (_make_hud() if enabled else None),
        default_action_policy=StructuredOutputPolicy.name,
        default_supervisor=MilestoneSupervisorPolicy.name,
    )
