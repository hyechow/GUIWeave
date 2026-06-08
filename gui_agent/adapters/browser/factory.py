"""Browser adapter factory: the one place browser construction is wired together.

Builds a :class:`gui_agent.core.factory.PlatformBundle` whose callables
construct the browser session (Chrome over CDP), executor, perception, action
policy and supervisor. Core orchestration receives the neutral bundle and never
imports these classes directly. Mirrors ``adapters/iphone/factory.py``.

SCROLL-COLLECT NOT YET SUPPORTED
--------------------------------
The iphone-shaped scroll/stitch bundle fields back the runner's scroll-collect /
stitching branch, which the browser adapter does NOT implement yet:
  - ``apply_scroll_profile`` is the identity (no per-platform scroll profiles).
  - ``make_scroll_probe`` / ``make_stitch_accumulator`` / ``robust_shift`` /
    ``gray_u8`` raise ``NotImplementedError``.
WARNING: this branch IS reachable on ordinary browser goals — the milestone
supervisor plans completion_strategy='scroll_until_boundary' for information-
gathering goals (e.g. "read all items on this page"), so such a goal raises
mid-run. Until browser collection is built (the neutral stitch algos
robust_shift/gray_u8/StitchAccumulator can be reused; only the scroll-probe is
iphone-specific), restrict the browser platform to direct-action goals.
The status reporter is a translucent HUD floating over the Chrome window (the
neutral core AgentHUD; macOS-host only), enabled by the --hud flag.

ACTION VISUALIZATION
--------------------
``make_action_visualizer`` provides a live DOM overlay (``BrowserActionVisualizer``)
that the agent loop flashes on the page at each action's location, so a human
watching the real Chrome sees where/what the agent clicks/types/scrolls. It is the
browser implementation of the neutral ``ActionVisualizer`` contract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from gui_agent.core.factory import PlatformBundle

if TYPE_CHECKING:
    from gui_agent.core.contracts import ActionPolicy, SupervisorPolicy


# Registries (mirror the iphone adapter shape). Browser is vision-only with a
# single action policy today; the default supervisor is structure-neutral milestone.
_POLICY_NAMES: tuple[str, ...] = ("browser_vision",)
_SUPERVISOR_NAMES: tuple[str, ...] = ("milestone",)


def _build_action_policy(name: str) -> "ActionPolicy":
    from gui_agent.adapters.browser.policies import BrowserActionPolicy

    registry: dict[str, type] = {BrowserActionPolicy.name: BrowserActionPolicy}
    try:
        return registry[name]()
    except KeyError as exc:
        choices = ", ".join(sorted(registry))
        raise ValueError(f"未知策略 {name!r}，可选：{choices}") from exc


def _build_supervisor(name: str) -> "SupervisorPolicy":
    # Browser uses the structure-neutral milestone supervisor FRAMEWORK with its OWN
    # web-tuned prompts (adapters/browser/supervisor/milestone/prompts.py) injected —
    # it no longer borrows the iphone set. ⚠️ those prompts are a DRAFT and need real
    # browser-task A/B tuning. The iphone SimpleSupervisorPolicy is not offered here.
    from gui_agent.core.supervisor.milestone.policy import MilestoneSupervisorPolicy
    from gui_agent.adapters.browser.supervisor.milestone.prompts import BROWSER_MILESTONE_PROMPTS

    if name == MilestoneSupervisorPolicy.name:
        return MilestoneSupervisorPolicy(prompts=BROWSER_MILESTONE_PROMPTS)
    raise ValueError(f"未知监督者 {name!r}，可选：{MilestoneSupervisorPolicy.name}")


def _apply_scroll_profile(action: object, profile: object) -> object:
    """Identity: browser has no per-platform scroll profiles (no-op pass-through)."""
    return action


_SCROLL_COLLECT_MSG = (
    "browser scroll-collect not yet supported (the milestone supervisor planned "
    "completion_strategy='scroll_until_boundary'). Use a direct-action goal, or run "
    "such collection on the iphone platform. See the SCROLL-COLLECT note in "
    "adapters/browser/factory.py."
)


def _make_scroll_probe(session: object, executor: object, log_dir: object) -> object:
    """Raised if a scroll_until_boundary milestone reaches the runner's collection branch (browser collection not yet supported)."""
    raise NotImplementedError(_SCROLL_COLLECT_MSG)


def _make_stitch_accumulator(*args: object, **kwargs: object) -> object:
    """Raised if a scroll_until_boundary milestone reaches the runner's collection branch (browser collection not yet supported)."""
    raise NotImplementedError(_SCROLL_COLLECT_MSG)


def _robust_shift(*args: object, **kwargs: object) -> object:
    """Raised if a scroll_until_boundary milestone reaches the runner's collection branch (browser collection not yet supported)."""
    raise NotImplementedError(_SCROLL_COLLECT_MSG)


def _gray_u8(png_bytes: bytes) -> object:
    """Raised if a scroll_until_boundary milestone reaches the runner's collection branch (browser collection not yet supported)."""
    raise NotImplementedError(_SCROLL_COLLECT_MSG)


def _find_chrome_window() -> "tuple[int, int, int, int] | None":
    """(x, y, w, h) screen rect of the largest on-screen Google Chrome window via
    CGWindowList — a pre-connect best-effort guess for HUD placement (refined to the
    exact CDP window rect once the agent connects). None if not found / unavailable.
    macOS-host only (Quartz); imported lazily so the browser bundle stays Quartz-free
    unless the HUD is actually requested."""
    try:
        from Quartz import (  # type: ignore
            CGWindowListCopyWindowInfo,
            kCGNullWindowID,
            kCGWindowListOptionOnScreenOnly,
        )
    except Exception:
        return None
    wins = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID) or []
    best: "tuple[int, int, int, int] | None" = None
    best_area = 0
    for w in wins:
        if "chrome" not in str(w.get("kCGWindowOwnerName", "")).lower():
            continue
        b = w.get("kCGWindowBounds", {})
        ww, wh = int(b.get("Width", 0)), int(b.get("Height", 0))
        if ww > 200 and wh > 200 and ww * wh > best_area:
            best_area = ww * wh
            best = (int(b.get("X", 0)), int(b.get("Y", 0)), ww, wh)
    return best


def _make_browser_hud() -> object:
    """Translucent status HUD floating over the Chrome window (the browser status
    reporter): a horizontally-centered bar low in the window (≈ iOS dock height).
    It floats over the page but, being an OS overlay, never enters the agent's
    Page.captureScreenshot perception. Positioned best-effort pre-connect via
    CGWindowList; the runner repositions it to the exact CDP window rect once
    connected (both use core.hud.dock_rect, so the placement matches)."""
    from gui_agent.core.hud import AgentHUD, dock_rect

    rect = _find_chrome_window()
    hx, hy, hw, hh = dock_rect(*rect) if rect else (140, 600, 360, 56)
    return AgentHUD(origin=(hx, hy), width=hw, height=hh, alpha=0.82)


def _make_action_visualizer(session: object) -> object:
    """The browser ActionVisualizer.

    Default: ``BrowserCursorVisualizer`` — reuses the iphone ``agent_cursor`` OS
    overlay (blue arrow over the real Chrome window). It draws OUTSIDE the page, so
    it never pollutes the agent's own screenshot perception, and it is immune to the
    connect_over_cdp binding that hangs ``page.evaluate``. macOS-host only.

    Set ``BROWSER_VISUALIZER=dom`` for the host-agnostic in-page DOM overlay fallback
    (``BrowserActionVisualizer``) — useful if the browser adapter ever runs off macOS.
    """
    import os

    if (os.environ.get("BROWSER_VISUALIZER") or "cursor").lower() == "dom":
        from gui_agent.adapters.browser.visualizer import BrowserActionVisualizer

        return BrowserActionVisualizer(session)
    from gui_agent.adapters.browser.visualizer import BrowserCursorVisualizer

    return BrowserCursorVisualizer(session)


def build_browser_bundle(
    *,
    backend: Optional[str] = None,
    cdp_url: Optional[str] = None,
    start_url: Optional[str] = None,
    **_ignored: object,
) -> PlatformBundle:
    """Construct the browser PlatformBundle.

    ``backend`` is accepted for signature parity with the iphone factory (no
    browser backends today). ``cdp_url`` / ``start_url`` flow through to the
    session (default CDP http://localhost:9222, overridable via env CHROME_CDP_URL).
    """
    from gui_agent.adapters.browser.executor import BrowserExecutor
    from gui_agent.adapters.browser.perception import BrowserPerception, BrowserSession

    return PlatformBundle(
        platform="browser",
        open_session=lambda: BrowserSession(cdp_url=cdp_url, start_url=start_url),
        make_executor=lambda session: BrowserExecutor(session),
        make_perception=lambda session, png_path: BrowserPerception(session, png_path),
        make_action_policy=_build_action_policy,
        make_supervisor=_build_supervisor,
        make_status_reporter=lambda enabled: (_make_browser_hud() if enabled else None),
        make_action_visualizer=_make_action_visualizer,
        make_scroll_probe=_make_scroll_probe,
        apply_scroll_profile=_apply_scroll_profile,
        make_stitch_accumulator=_make_stitch_accumulator,
        robust_shift=_robust_shift,
        gray_u8=_gray_u8,
        default_action_policy="browser_vision",
        default_supervisor="milestone",
        action_policy_choices=_POLICY_NAMES,
        supervisor_choices=_SUPERVISOR_NAMES,
    )
