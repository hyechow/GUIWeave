"""Platform factory: the single seam where core obtains a platform's adapter bundle.

Core orchestration depends on this module + ``gui_agent.core.contracts`` only.
``build_platform`` NEVER imports ``gui_agent.adapters.*`` at module top -- it
dispatches by name with a lazy import inside the function body, so importing
``core.factory`` pulls in no adapter (preserving the leaf invariant) and core
stays adapter-free. The concrete wiring for each platform lives in that
platform's ``adapters/<plat>/factory.py``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, ContextManager, Optional

if TYPE_CHECKING:
    from contextlib import AbstractContextManager
    from pathlib import Path

    from gui_agent.core.contracts import (
        ActionPolicy,
        ActionVisualizer,
        Perception,
        PerceptionSession,
        SupervisorPolicy,
    )


@dataclass(frozen=True)
class PlatformBundle:
    """Everything core orchestration needs to drive one platform, expressed as
    callables that return contract-typed objects. No adapter type appears in this
    signature -- the bundle is the platform-neutral handle the agent loop holds.
    """

    platform: str
    open_session: Callable[[], "ContextManager[PerceptionSession]"]
    make_executor: Callable[["PerceptionSession"], object]
    make_perception: Callable[["PerceptionSession", "Path"], "Perception"]
    make_action_policy: Callable[[str], "ActionPolicy"]
    make_supervisor: Callable[[str], "SupervisorPolicy"]
    make_status_reporter: Callable[[bool], "Optional[AbstractContextManager]"]
    # Optional cross-platform action / cursor visualization, constructed per
    # session. Returns None when the platform has no visualizer (the agent loop
    # then skips it). iphone: None for now (agent_cursor overlay is a follow-up);
    # browser: a live DOM overlay; android: None.
    make_action_visualizer: Callable[["PerceptionSession"], "Optional[ActionVisualizer]"]
    # Scroll-collect helpers the agent loop needs (iphone-specific objects today,
    # typed as object so this neutral signature carries no adapter type).
    make_scroll_probe: Callable[["PerceptionSession", object, "Path"], object]
    apply_scroll_profile: Callable[[object, object], object]
    make_stitch_accumulator: Callable[..., object]
    robust_shift: Callable[..., object]
    gray_u8: Callable[[bytes], object]
    default_action_policy: str
    default_supervisor: str
    action_policy_choices: tuple[str, ...]
    supervisor_choices: tuple[str, ...]


def build_platform(
    platform: Optional[str] = None,
    *,
    backend: Optional[str] = None,
    **kwargs: object,
) -> PlatformBundle:
    """Return the adapter bundle for the selected platform.

    Resolution: ``platform`` arg -> env ``AGENT_PLATFORM`` -> ``"iphone"``.
    Adapter modules are imported lazily inside the matched branch so
    ``core.factory`` itself stays adapter-free.
    """
    name = (platform or os.environ.get("AGENT_PLATFORM") or "iphone").lower()
    if name == "iphone":
        from gui_agent.adapters.iphone.factory import build_iphone_bundle

        return build_iphone_bundle(backend=backend, **kwargs)
    if name == "browser":
        from gui_agent.adapters.browser.factory import build_browser_bundle

        return build_browser_bundle(backend=backend, **kwargs)
    raise ValueError(f"unknown platform {name!r}; registered: iphone, browser")
