"""Browser adapter factory: the one place browser construction is wired together.

Builds a :class:`gui_agent.core.runtime.factory.PlatformBundle` whose callables
construct the browser session (Chrome over CDP), executor, perception, action
policy and supervisor. Core orchestration receives the neutral bundle and never
imports these classes directly. Mirrors ``adapters/iphone/factory.py``.

SCROLL-COLLECT
--------------
The scroll/stitch bundle fields back the runner's scroll-collect / stitching branch
(reached when the milestone supervisor plans completion_strategy='scroll_until_boundary'
for information-gathering goals). Browser collection IS implemented:
  - ``make_stitch_accumulator`` / ``robust_shift`` / ``gray_u8`` reuse the NEUTRAL
    ``gui_agent.core.vision.stitch`` algos with the browser content band (whole frame, no
    device-frame mask).
  - ``make_scroll_probe`` / ``apply_scroll_profile`` use the trivial
    ``adapters/browser/scroll_probe.py`` (browser wheel scroll is deterministic, so
    the probe just scrolls once and verifies vertical progress).
⚠️ DRAFT: validated on simple lists; the stitch tuning (chunk size / overlap / shift
thresholds) inherits iphone defaults and may need browser A/B tuning on long pages.
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

from gui_agent.core.runtime.factory import PlatformBundle, SetupCheckResult

if TYPE_CHECKING:
    from gui_agent.core.runtime.contracts import ActionPolicy, SupervisorPolicy


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


# Browser screenshots are full-page content (no device frame / iOS status bar), so
# the neutral stitch runs with the WHOLE frame as content band and no frame mask.
_BROWSER_CONTENT_TOP, _BROWSER_CONTENT_BOT = 0.0, 1.0


def _apply_scroll_profile(action: object, profile: object) -> object:
    """Pin a fresh scroll action to a verified scroll point (runner cached path)."""
    from gui_agent.adapters.browser.scroll_probe import apply_profile

    return apply_profile(action, profile)


def _make_scroll_probe(session: object, executor: object, log_dir: object) -> object:
    """Trivial browser scroll-probe (wheel scroll is deterministic — one scroll +
    shift check). See adapters/browser/scroll_probe.py."""
    from gui_agent.adapters.browser.scroll_probe import BrowserScrollProbe

    return BrowserScrollProbe(session, executor, log_dir)


def _make_stitch_accumulator(*args: object, **kwargs: object) -> object:
    """Neutral core StitchAccumulator with the browser content band (whole frame)."""
    from gui_agent.core.vision.stitch import StitchAccumulator

    kwargs.setdefault("content_top", _BROWSER_CONTENT_TOP)
    kwargs.setdefault("content_bot", _BROWSER_CONTENT_BOT)
    kwargs.setdefault("frame_mask", None)
    return StitchAccumulator(*args, **kwargs)


def _robust_shift(*args: object, **kwargs: object) -> object:
    """Vertical shift on full-frame browser screenshots (whole-frame band, no mask)."""
    from gui_agent.adapters.browser.scroll_probe import browser_robust_shift

    return browser_robust_shift(*args, **kwargs)


def _gray_u8(png_bytes: bytes) -> object:
    from gui_agent.core.vision.stitch import _gray_u8 as _impl

    return _impl(png_bytes)


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


def _setup_check(cdp_url: "Optional[str]") -> SetupCheckResult:
    """Pre-session environment check: is the Chrome CDP endpoint reachable? The whole
    browser adapter is connect_over_cdp on an already-running Chrome started with
    remote debugging (bin/launch_chrome_cdp). A clean HTTP probe of ``/json/version``
    catches a not-started / wrong-port Chrome early, with the fix in the message,
    instead of hanging inside connect_over_cdp."""
    import json
    import os
    import urllib.request

    url = cdp_url or os.environ.get("CHROME_CDP_URL") or "http://localhost:9222"
    try:
        with urllib.request.urlopen(f"{url}/json/version", timeout=3) as resp:
            info = json.loads(resp.read().decode())
    except Exception as exc:  # noqa: BLE001
        return SetupCheckResult(
            ok=False,
            summary=f"Chrome CDP 不可用 @ {url}",
            lines=(
                f"  ✗ 连不上 CDP：{exc}",
                "    先跑 bin/launch_chrome_cdp 起一个带远程调试的 Chrome（独立 profile）",
            ),
        )
    return SetupCheckResult(
        ok=True,
        summary="Chrome CDP 已就绪",
        lines=(f"  ✓ Chrome CDP 可用 @ {url}（{info.get('Browser', '?')}）",),
    )


def _make_browser_hud() -> object:
    """Translucent status HUD floating over the Chrome window (the browser status
    reporter): a horizontally-centered bar low in the window (≈ iOS dock height).
    It floats over the page but, being an OS overlay, never enters the agent's
    Page.captureScreenshot perception. Positioned best-effort pre-connect via
    CGWindowList; the runner repositions it to the exact CDP window rect once
    connected (both use core.ui.hud.dock_rect, so the placement matches)."""
    from gui_agent.core.ui.hud import AgentHUD, dock_rect

    rect = _find_chrome_window()
    hx, hy, hw, hh = dock_rect(*rect) if rect else (140, 600, 600, 150)
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
        setup_check=lambda: _setup_check(cdp_url),
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
