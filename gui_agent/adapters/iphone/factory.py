"""iPhone adapter factory: the one place iPhone construction is wired together.

Builds a :class:`gui_agent.core.factory.PlatformBundle` whose callables
construct the iPhone-mirroring session, executor, perception, action policy,
supervisor and HUD. This is the only module allowed to know how all the iPhone
pieces fit together; core orchestration receives the neutral bundle and never
imports these classes directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from gui_agent.core.factory import PlatformBundle

from gui_agent.adapters.iphone.executor import ActionExecutor
from gui_agent.adapters.iphone.perception import LivePerception, LivePhoneSession
from gui_agent.adapters.iphone.policies.structured_output import StructuredOutputPolicy
from gui_agent.adapters.iphone.supervisor.simple import SimpleSupervisorPolicy
from gui_agent.core.supervisor.milestone.policy import MilestoneSupervisorPolicy

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from gui_agent.core.contracts import ActionPolicy, SupervisorPolicy


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
    from gui_agent.adapters.iphone.hud import AgentHUD

    return AgentHUD()


def _make_scroll_probe(phone: object, executor: object, log_dir: object) -> object:
    from gui_agent.adapters.iphone.scroll_probe import ScrollProbe

    return ScrollProbe(phone, executor, log_dir)


def _apply_scroll_profile(action: object, profile: object) -> object:
    from gui_agent.adapters.iphone.scroll_probe import apply_profile

    return apply_profile(action, profile)


def _make_stitch_accumulator(*args: object, **kwargs: object) -> object:
    from gui_agent.adapters.iphone.stitch import StitchAccumulator

    return StitchAccumulator(*args, **kwargs)


def _robust_shift(*args: object, **kwargs: object) -> object:
    from gui_agent.adapters.iphone.stitch import robust_shift

    return robust_shift(*args, **kwargs)


def _gray_u8(png_bytes: bytes) -> object:
    from gui_agent.adapters.iphone.stitch import _gray_u8 as _impl

    return _impl(png_bytes)


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
        # No action visualizer yet: wiring the existing agent_cursor blue-arrow
        # overlay (sck/agent_cursor.py) into an ActionVisualizer is the iphone
        # follow-up; until then the agent loop simply skips visualization.
        make_action_visualizer=lambda session: None,
        make_scroll_probe=_make_scroll_probe,
        apply_scroll_profile=_apply_scroll_profile,
        make_stitch_accumulator=_make_stitch_accumulator,
        robust_shift=_robust_shift,
        gray_u8=_gray_u8,
        default_action_policy=StructuredOutputPolicy.name,
        default_supervisor=MilestoneSupervisorPolicy.name,
        action_policy_choices=tuple(sorted(_POLICIES)),
        supervisor_choices=tuple(sorted(_SUPERVISORS)),
    )
