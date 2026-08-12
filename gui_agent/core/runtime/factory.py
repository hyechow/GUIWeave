"""Tool Agent's platform factory and adapter boundary."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, ContextManager, Optional

from gui_agent.core.runtime.platforms import PLATFORMS

if TYPE_CHECKING:
    from pathlib import Path

    from gui_agent.core.runtime.contracts import (
        ActionVisualizer,
        Perception,
        PerceptionSession,
    )
    from gui_agent.core.runtime.clock import PlatformTimeSnapshot


@dataclass(frozen=True)
class SetupCheckResult:
    """Result of a platform preflight check."""

    ok: bool
    summary: str
    lines: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlatformBundle:
    """Everything Tool Agent needs to drive one platform.

    Master/Worker orchestration owns planning and action choice. Adapters own
    sessions, perception, action validation, execution and optional visualization.
    Planning policies intentionally do not cross this adapter seam.
    """

    platform: str
    open_session: Callable[[], "ContextManager[PerceptionSession]"]
    setup_check: Callable[[], SetupCheckResult]
    make_executor: Callable[["PerceptionSession"], object]
    make_action: Callable[[dict[str, object]], object]
    make_perception: Callable[["PerceptionSession", "Path"], "Perception"]
    make_status_reporter: Callable[[bool], object | None]
    make_action_visualizer: Callable[
        ["PerceptionSession"], "Optional[ActionVisualizer]"
    ]
    read_time: Callable[["PerceptionSession"], "PlatformTimeSnapshot"]
    tool_agent_capabilities: tuple[str, ...]


def build_platform(
    platform: Optional[str] = None,
    *,
    backend: Optional[str] = None,
    **kwargs: object,
) -> PlatformBundle:
    """Return a Browser, Android, or iPhone Tool Agent adapter bundle."""

    name = (platform or os.environ.get("AGENT_PLATFORM") or "browser").lower()
    if name == "browser":
        from gui_agent.adapters.browser.factory import build_browser_bundle

        return build_browser_bundle(backend=backend, **kwargs)
    if name == "android":
        from gui_agent.adapters.android.factory import build_android_bundle

        return build_android_bundle(backend=backend, **kwargs)
    if name == "iphone":
        from gui_agent.adapters.iphone.factory import build_iphone_bundle

        return build_iphone_bundle(backend=backend, **kwargs)
    raise ValueError(f"unknown platform {name!r}; registered: {', '.join(PLATFORMS)}")


__all__ = ["PlatformBundle", "SetupCheckResult", "build_platform"]
