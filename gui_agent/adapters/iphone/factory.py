"""iPhone adapter factory: the one place iPhone construction is wired together.

Builds a :class:`gui_agent.core.runtime.factory.PlatformBundle` whose callables
construct the iPhone-mirroring session, executor, perception, action policy,
supervisor and HUD. This is the only module allowed to know how all the iPhone
pieces fit together; core orchestration receives the neutral bundle and never
imports these classes directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from gui_agent.core.runtime.factory import PlatformBundle, SetupCheckResult

from gui_agent.adapters.iphone.executor import ActionExecutor
from gui_agent.adapters.iphone.perception import LivePerception, LivePhoneSession
from gui_agent.adapters.iphone.policies.structured_output import StructuredOutputPolicy
from gui_agent.core.supervisor.statement.policy import StatementSupervisorPolicy

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from gui_agent.core.runtime.contracts import ActionPolicy, SupervisorPolicy


# Registries (mirrors of the legacy dicts in runner.py; runner keeps its own copy
# until the Step-4 flip routes selection through the bundle).
_POLICIES: dict[str, type] = {
    StructuredOutputPolicy.name: StructuredOutputPolicy,
}
_SUPERVISOR_NAMES: tuple[str, ...] = (StatementSupervisorPolicy.name,)


def _build_action_policy(name: str) -> "ActionPolicy":
    try:
        return _POLICIES[name]()
    except KeyError as exc:
        choices = ", ".join(sorted(_POLICIES))
        raise ValueError(f"未知策略 {name!r}，可选：{choices}") from exc


def _build_supervisor(name: str) -> "SupervisorPolicy":
    from gui_agent.adapters.iphone.supervisor.statement.prompts import (
        IPHONE_STATEMENT_PROMPTS,
    )

    if name == StatementSupervisorPolicy.name:
        return StatementSupervisorPolicy(prompts=IPHONE_STATEMENT_PROMPTS)
    choices = ", ".join(_SUPERVISOR_NAMES)
    raise ValueError(f"未知监督者 {name!r}，可选：{choices}")


def _make_hud() -> "AbstractContextManager":
    from gui_agent.adapters.iphone.hud import make_iphone_hud

    return make_iphone_hud()


def _setup_check() -> SetupCheckResult:
    """Pre-session environment check: is the iPhone Mirroring window open? Everything
    iphone does (SCK capture, zero-preempt input, window geometry) hangs off that
    macOS window, so its absence is a hard block worth catching early with a clear
    message rather than failing deep in the session/SCK layer."""
    from gui_agent.adapters.iphone.perception import mirroring_window_bounds

    rect = mirroring_window_bounds()
    if rect is None:
        return SetupCheckResult(
            ok=False,
            summary="iPhone 镜像窗口未打开",
            lines=("  ✗ 未找到 iPhone Mirroring 窗口——请先打开「iPhone 镜像」并保持窗口可见",),
        )
    _, _, w, h = rect
    return SetupCheckResult(
        ok=True,
        summary="iPhone 镜像已就绪",
        lines=(f"  ✓ iPhone Mirroring 窗口已打开 ({int(w)}x{int(h)})",),
    )


def _prepare_vision_prompt_png(png_bytes: bytes) -> bytes:
    from gui_agent.core.policies.base import resize_to_logical_png

    return resize_to_logical_png(png_bytes)


def build_iphone_bundle(*, backend: Optional[str] = None, **_ignored: object) -> PlatformBundle:
    """Construct the iPhone PlatformBundle. ``backend`` is the daemon/mirroir knob
    (passed through to LivePhoneSession; None lets it fall back to AGENT_MODE)."""
    from gui_agent.adapters.iphone.actions import IPhoneAction

    return PlatformBundle(
        platform="iphone",
        open_session=lambda: LivePhoneSession(backend=backend),
        setup_check=_setup_check,
        make_executor=lambda phone: ActionExecutor(phone),
        make_action=lambda payload: IPhoneAction.model_validate(payload),
        make_perception=lambda phone, png_path: LivePerception(phone, png_path),
        make_action_policy=_build_action_policy,
        make_supervisor=_build_supervisor,
        make_status_reporter=lambda enabled: (_make_hud() if enabled else None),
        # None BY DESIGN — not a TODO. iphone drives its agent_cursor overlay at the
        # DEVICE layer (MirrorDaemonClient.tap/scroll/drag), which is the only place
        # that has (a) the POST-snap coordinates (OCR/YOLO snapping happens inside
        # ActionExecutor.execute, AFTER the runner's pre-execute show_action hook) and
        # (b) coverage of every dispatched action path.
        # Returning a runner-driven ActionVisualizer here would regress both (pre-snap
        # cursor + no cursor on scroll-collect). The seam stays unified at the contract
        # + renderer level (browser reuses the same agent_cursor); the driving layer
        # differs because that is iphone's correct home. See ActionVisualizer docstring.
        make_action_visualizer=lambda session: None,
        prepare_vision_prompt_png=_prepare_vision_prompt_png,
        move_collection=None,
        validate_collection_action=None,
        default_action_policy=StructuredOutputPolicy.name,
        default_supervisor=StatementSupervisorPolicy.name,
        action_policy_choices=tuple(sorted(_POLICIES)),
        supervisor_choices=_SUPERVISOR_NAMES,
    )
