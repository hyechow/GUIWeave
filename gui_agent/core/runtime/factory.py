"""Platform factory: the single seam where core obtains a platform's adapter bundle.

Core orchestration depends on this module + ``gui_agent.core.runtime.contracts`` only.
``build_platform`` NEVER imports ``gui_agent.adapters.*`` at module top -- it
dispatches by name with a lazy import inside the function body, so importing
``core.runtime.factory`` pulls in no adapter (preserving the leaf invariant) and core
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

    from gui_agent.core.runtime.contracts import (
        ActionPolicy,
        ActionVisualizer,
        Perception,
        PerceptionSession,
        SupervisorPolicy,
    )


@dataclass(frozen=True)
class SetupCheckResult:
    """Outcome of a platform's pre-session environment check (``PlatformBundle.setup_check``).

    ``ok`` False means a BLOCKING precondition failed — the runner aborts with ``summary``
    BEFORE opening the session, instead of crashing deep inside ``connect``. ``lines`` are
    human-readable per-check notes (passes ✓ and non-blocking warnings ⚠) printed before the
    run. A platform that does any one-time SETUP (e.g. android switching the IME to
    ADBKeyboard) does it here, in the check — the check is the environment-preparation seam,
    deliberately kept OUT of the per-turn execution path.
    """

    ok: bool
    summary: str
    lines: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlatformBundle:
    """Everything core orchestration needs to drive one platform, expressed as
    callables that return contract-typed objects. No adapter type appears in this
    signature -- the bundle is the platform-neutral handle the agent loop holds.
    """

    platform: str
    open_session: Callable[[], "ContextManager[PerceptionSession]"]
    # Pre-session environment check, run ONCE by the runner before ``open_session`` so a
    # missing precondition fails early with a clear message instead of crashing deep in
    # connect. Each adapter probes (and, where needed, prepares) its OWN backend:
    #   iphone : the iPhone Mirroring window is open.
    #   browser: the Chrome CDP endpoint is reachable.
    #   android: an adb device is reachable; scrcpy present; ADBKeyboard installed → switch
    #            the device IME to ADBKeyboard (the Chinese-input setup lives here, not in
    #            the per-connect path).
    setup_check: Callable[[], "SetupCheckResult"]
    make_executor: Callable[["PerceptionSession"], object]
    make_perception: Callable[["PerceptionSession", "Path"], "Perception"]
    make_action_policy: Callable[[str], "ActionPolicy"]
    make_supervisor: Callable[[str], "SupervisorPolicy"]
    make_status_reporter: Callable[[bool], "Optional[AbstractContextManager]"]
    # Optional cross-platform action / cursor visualization, constructed per
    # session. Returns None when the platform has no runner-driven visualizer (the
    # agent loop then skips the show_action hook).
    #   browser: agent_cursor OS overlay (BrowserCursorVisualizer; DOM fallback).
    #   iphone : None BY DESIGN — its agent_cursor is driven at the device layer
    #            (post-snap coords + all-path coverage); see adapters/iphone/factory.
    #   android: None (TBD).
    make_action_visualizer: Callable[["PerceptionSession"], "Optional[ActionVisualizer]"]
    # Platform-specific image normalization for shared vision-LLM prompts. iPhone
    # screenshots are Retina-sized and should be downsampled to logical pixels;
    # browser/android observations are already in their reasoning coordinate space.
    prepare_vision_prompt_png: Callable[[bytes], bytes]
    # Optional deterministic movement for one adapter-bound collection.  Core
    # supplies the exact current-frame candidate; adapters may only move that
    # surface forward/backward and never choose a business collection.
    move_collection: Callable[["PerceptionSession", dict, str], bool] | None
    validate_collection_action: Callable[["PerceptionSession", dict, object, str], bool] | None
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
    ``core.runtime.factory`` itself stays adapter-free.
    """
    name = (platform or os.environ.get("AGENT_PLATFORM") or "iphone").lower()
    if name == "iphone":
        from gui_agent.adapters.iphone.factory import build_iphone_bundle

        return build_iphone_bundle(backend=backend, **kwargs)
    if name == "browser":
        from gui_agent.adapters.browser.factory import build_browser_bundle

        return build_browser_bundle(backend=backend, **kwargs)
    if name == "android":
        from gui_agent.adapters.android.factory import build_android_bundle

        return build_android_bundle(backend=backend, **kwargs)
    raise ValueError(f"unknown platform {name!r}; registered: iphone, browser, android")
