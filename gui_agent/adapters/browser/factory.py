"""Browser adapter factory: the one place browser construction is wired together.

Builds a :class:`gui_agent.core.runtime.factory.PlatformBundle` whose callables
construct the browser session (Chrome over CDP), executor and perception for
Tool Agent's Master/Worker runtime.

OBSERVATION MODEL
-----------------
The current frame is the complete visual input. Scrolling remains an ordinary,
journalled LLM action; the adapter holds no hidden cross-frame visual state.
The status reporter is a translucent HUD floating over the Chrome window (the
neutral core AgentHUD; macOS-host only), enabled by the --hud flag.

ACTION VISUALIZATION
--------------------
``make_action_visualizer`` provides ``BrowserCursorVisualizer``, which drives the
shared ``agent_cursor`` OS overlay over the real Chrome window at each action's
location, so a human watching the real Chrome sees where/what the agent clicks/
types/scrolls. It is the browser implementation of the neutral ``ActionVisualizer``
contract.
"""

from __future__ import annotations

from typing import Optional

from gui_agent.core.runtime.factory import PlatformBundle, SetupCheckResult


def _probe_headless_chromium() -> str:
    """Launch and close Chromium once so preflight covers installed browser assets."""

    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            return playwright.chromium.executable_path
        finally:
            browser.close()


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


def _setup_check(cdp_url: "Optional[str]", *, headless: bool | None = None) -> SetupCheckResult:
    """Pre-session environment check: is the Chrome CDP endpoint reachable? The whole
    browser adapter is connect_over_cdp on an already-running Chrome started with
    remote debugging (bin/launch_chrome_cdp). A clean HTTP probe of ``/json/version``
    catches a not-started / wrong-port Chrome early, with the fix in the message,
    instead of hanging inside connect_over_cdp."""
    import json
    import os
    import urllib.request

    if _resolve_headless(headless):
        try:
            executable = _probe_headless_chromium()
        except Exception as exc:  # noqa: BLE001 - return actionable setup failure
            return SetupCheckResult(
                ok=False,
                summary="Playwright Chromium 不可用",
                lines=(
                    f"  ✗ headless Chromium 启动失败：{exc}",
                    "    运行 `uv run playwright install chromium` 后重试",
                ),
            )
        return SetupCheckResult(
            ok=True,
            summary="Playwright Chromium 已就绪",
            lines=(
                f"  ✓ headless Chromium 可启动: {executable}",
                "  ✓ headless 模式：无需外部 Chrome CDP",
            ),
        )

    url = cdp_url or os.environ.get("CHROME_CDP_URL") or "http://localhost:9222"
    try:
        from gui_agent.adapters.browser.device import _direct_cdp_host

        if _direct_cdp_host(url):
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            request = opener.open
        else:
            request = urllib.request.urlopen
        with request(f"{url}/json/version", timeout=3) as resp:
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
    """The browser ActionVisualizer: ``BrowserCursorVisualizer`` — reuses the shared
    ``agent_cursor`` OS overlay (blue arrow over the real Chrome window). It draws OUTSIDE
    the page, so it never pollutes the agent's own screenshot perception, and it is immune
    to the connect_over_cdp binding that hangs ``page.evaluate``. macOS-host only.

    None in headless mode — the unified headless switch is the only visibility control
    (the loop also skips the visualizer when headless; this gate covers callers like the
    WebArena harness that don't thread the loop's headless param)."""
    if _resolve_headless(getattr(getattr(session, "client", None), "headless", None)):
        return None
    from gui_agent.adapters.browser.visualizer import BrowserCursorVisualizer

    return BrowserCursorVisualizer(session)


def build_browser_bundle(
    *,
    backend: Optional[str] = None,
    cdp_url: Optional[str] = None,
    start_url: Optional[str] = None,
    headless: bool | None = None,
    user_data_dir: Optional[str] = None,
    **_ignored: object,
) -> PlatformBundle:
    """Construct the browser PlatformBundle.

    ``backend`` is reserved for future adapter backends. ``cdp_url`` / ``start_url`` / ``headless`` /
    ``user_data_dir`` flow through to the session. CDP defaults to
    http://localhost:9222, overridable via env CHROME_CDP_URL; headless mode
    launches Chromium directly and can keep login state in ``user_data_dir``.
    """
    from gui_agent.adapters.browser.actions import BrowserAction
    from gui_agent.adapters.browser.executor import BrowserExecutor
    from gui_agent.adapters.browser.perception import BrowserPerception, BrowserSession

    return PlatformBundle(
        platform="browser",
        open_session=lambda: BrowserSession(
            cdp_url=cdp_url,
            start_url=start_url,
            headless=headless,
            user_data_dir=user_data_dir,
        ),
        setup_check=lambda: _setup_check(cdp_url, headless=headless),
        make_executor=lambda session: BrowserExecutor(session),
        make_action=lambda payload: BrowserAction.model_validate(payload),
        make_perception=lambda session, png_path: BrowserPerception(session, png_path),
        make_status_reporter=lambda enabled: (_make_browser_hud() if enabled else None),
        make_action_visualizer=_make_action_visualizer,
        read_time=lambda session: session.platform_time(),
        tool_agent_capabilities=(
            "tap",
            "type",
            "clear_text",
            "press_enter",
            "scroll",
            "select_option",
            "open_url",
            "back",
        ),
    )


def _resolve_headless(headless: bool | None) -> bool:
    if headless is not None:
        return headless
    import os

    # AGENT_HEADLESS is the unified cross-platform switch (set by --headless);
    # BROWSER_HEADLESS / WEB_ARENA_HEADLESS remain as browser-specific aliases (bin/webarena).
    raw = (
        os.environ.get("AGENT_HEADLESS")
        or os.environ.get("BROWSER_HEADLESS")
        or os.environ.get("WEB_ARENA_HEADLESS")
    )
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}
