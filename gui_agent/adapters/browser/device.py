"""Playwright/CDP I/O backend implementing gui_agent.core.contracts.Device.

``PlaywrightDevice`` attaches to a user's running Chrome over the Chrome DevTools
Protocol (CDP) and drives the active page through a desktop pointer + keyboard. It
satisfies the neutral ``Device`` Protocol (connect/close/screenshot/tap/type_text/
drag/press_home) AND the optional ``ScrollableDevice`` capability (scroll), plus
the browser-only extras ``navigate(url)`` / ``go_back()``.

Coordinates passed to ``tap`` / ``drag`` / ``scroll`` are VIEWPORT PIXELS — the
executor denormalizes 0-1000 -> px using ``viewport_size`` before calling here
(mirroring how the iphone executor denormalizes via ``logical_xy`` / WIN_W / WIN_H).

Every input method returns a status ``str`` (the executor scans it for
"paused" / "interrupted" / "failed"). ``close()`` detaches from the user's Chrome
via ``playwright.stop()`` WITHOUT closing their browser.
"""

from __future__ import annotations

import os
from typing import Optional

# Default viewport used to denormalize coordinates and for screenshots when the
# real page viewport cannot be read (CDP pages frequently report viewport_size as
# None for the user's existing tab). Typical laptop content size.
_DEFAULT_VIEWPORT_W = 1280
_DEFAULT_VIEWPORT_H = 800

# Pixels of wheel scroll per unit of the iphone-style ``amount`` (amount=5 -> a
# bit under one screen). Keeps the neutral scroll(direction, amount=5, ...)
# signature identical to ScrollableDevice while mapping to real wheel deltas.
_SCROLL_PX_PER_AMOUNT = 120


class PlaywrightDevice:
    """Desktop-pointer web device over CDP. Vision-only; no DOM access here."""

    # Capability markers probed by core via hasattr/getattr (see contracts).
    # Browser scroll is real pointer-wheel; it is NOT zero-preempt (it drives the
    # user's actual Chrome), so we do NOT set zero_preempt.
    zero_preempt = False

    def __init__(
        self,
        cdp_url: Optional[str] = None,
        *,
        start_url: Optional[str] = None,
    ):
        # Resolution: explicit arg -> env CHROME_CDP_URL -> localhost:9222.
        self.cdp_url = cdp_url or os.environ.get("CHROME_CDP_URL") or "http://localhost:9222"
        self.start_url = start_url
        self._pw = None  # sync_playwright() handle (stop() on close)
        self._browser = None
        self._context = None
        self.page = None
        self._cdp = None  # cached raw CDP session for one-shot screenshots

    # ----- lifecycle -------------------------------------------------------
    def connect(self):
        """Attach to the running Chrome over CDP and bind the active page.

        Reuses the first existing context/page if present (the user's tab); only
        creates new ones when the attached browser has none.
        """
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.connect_over_cdp(self.cdp_url)
        contexts = self._browser.contexts
        self._context = contexts[0] if contexts else self._browser.new_context()
        pages = self._context.pages
        self.page = pages[0] if pages else self._context.new_page()
        return self

    def close(self):
        """Detach from Chrome WITHOUT closing the user's browser.

        Only stops the Playwright driver (which closes the CDP transport); the
        attached Chrome and its tabs keep running.
        """
        if self._pw is not None:
            try:
                self._pw.stop()
            finally:
                self._pw = None
                self._browser = None
                self._context = None
                self.page = None
                self._cdp = None

    # ----- viewport (for the executor's denormalization) -------------------
    @property
    def viewport_size(self) -> tuple[int, int]:
        """(width, height) in CSS px the executor uses to denormalize 0-1000 -> px.

        Reads window.innerWidth/innerHeight via a raw CDP ``Runtime.evaluate``. We do
        NOT use ``page.evaluate`` here: when the high-level execution-context binding
        fails over ``connect_over_cdp`` (Playwright<->Chrome version skew), every
        ``page.evaluate`` hangs forever — and ``page.viewport_size`` is usually None
        on a CDP-attached tab anyway. Falls back to a sane default on any failure.
        """
        try:
            res = self._cdp_send(
                "Runtime.evaluate",
                {
                    "expression": "({w: window.innerWidth, h: window.innerHeight})",
                    "returnByValue": True,
                },
            )
            val = (res.get("result") or {}).get("value") or {}
            w, h = int(val.get("w") or 0), int(val.get("h") or 0)
            if w and h:
                return w, h
        except Exception:
            pass
        return _DEFAULT_VIEWPORT_W, _DEFAULT_VIEWPORT_H

    # ----- perception ------------------------------------------------------
    def screenshot(self) -> bytes:
        """Return the current page as PNG bytes via a raw CDP one-shot capture.

        WHY NOT ``page.screenshot()``: the high-level wrapper (a) auto-waits for the
        page to stop navigating — a Google results page never looks "settled", so it
        retried to its timeout (the persistent ~25s post-Enter stall), and (b) hangs
        outright when the execution-context binding fails over ``connect_over_cdp``
        (Playwright<->Chrome version skew). Raw ``Page.captureScreenshot`` does
        neither: it grabs the current frame immediately, mid-navigation included.
        """
        try:
            return self._cdp_screenshot()
        except Exception:
            pass
        # Last-resort fallback (e.g. an occluded window where the CDP surface capture
        # could not get a frame): bring the tab forward and use the wrapper, bounded
        # so it can never hang the loop. On a binding-broken setup this also fails
        # fast rather than hanging — but the CDP primary above is the working path.
        page = self._require_page()
        try:
            page.bring_to_front()
        except Exception:
            pass
        return page.screenshot(timeout=8000)

    def _cdp_send(self, method: str, params: dict) -> dict:
        """Send a raw CDP command on the cached per-page session, rebuilding it once
        if it went stale (the page target changed). Raw CDP is used throughout for
        anything the high-level Playwright page API would route through the
        execution-context binding (evaluate / wrapper screenshot / viewport), which
        hangs when that binding fails over ``connect_over_cdp`` on a mismatched
        Chrome — whereas raw CDP keeps working."""
        page = self._require_page()
        if self._cdp is None:
            self._cdp = self._context.new_cdp_session(page)
        try:
            return self._cdp.send(method, params)
        except Exception:
            self._cdp = self._context.new_cdp_session(page)
            return self._cdp.send(method, params)

    def _cdp_screenshot(self) -> bytes:
        import base64

        result = self._cdp_send("Page.captureScreenshot", {"format": "png"})
        return base64.b64decode(result["data"])

    def eval_js(self, expression: str) -> object:
        """Best-effort JS eval via raw CDP ``Runtime.evaluate`` (NOT
        ``page.evaluate``, which hangs on a broken execution-context binding). Used
        by the action visualizer to draw its overlay. Returns the evaluated value
        (when ``returnByValue`` data is present) or None; never raises."""
        try:
            res = self._cdp_send(
                "Runtime.evaluate", {"expression": expression, "returnByValue": False}
            )
            return (res.get("result") or {}).get("value")
        except Exception:
            return None

    def window_bounds(self) -> tuple[int, int, int, int] | None:
        """(left, top, width, height) of the browser window in screen points (CSS
        px, top-left origin) via raw CDP ``Browser.getWindowForTarget``. Used by the
        screen-cursor visualizer to map page coords -> macOS screen coords. Returns
        None on any failure (cursor then skips this action)."""
        try:
            res = self._cdp_send("Browser.getWindowForTarget", {})
            b = res.get("bounds") or {}
            return int(b["left"]), int(b["top"]), int(b["width"]), int(b["height"])
        except Exception:
            return None

    # ----- input primitives ------------------------------------------------
    def tap(self, x: float, y: float) -> str:
        try:
            self._require_page().mouse.click(x, y)
        except Exception as exc:  # noqa: BLE001 — surface as status str for executor
            return f"failed: {exc}"
        return f"OK tap ({x:.0f},{y:.0f})"

    def type_text(self, text: str) -> str:
        try:
            self._require_page().keyboard.type(text)
        except Exception as exc:  # noqa: BLE001
            return f"failed: {exc}"
        return f"OK type {text!r}"

    def press_enter(self) -> str:
        try:
            self._require_page().keyboard.press("Enter")
        except Exception as exc:  # noqa: BLE001
            return f"failed: {exc}"
        return "OK enter"

    def clear_text(self) -> str:
        """Select-all + delete in the focused field (browser select-all is Meta/Ctrl-A)."""
        try:
            page = self._require_page()
            page.keyboard.press(f"{_select_all_modifier()}+a")
            page.keyboard.press("Delete")
        except Exception as exc:  # noqa: BLE001
            return f"failed: {exc}"
        return "OK clear"

    def select_all(self) -> str:
        """Select all text in the focused field (used before re-typing)."""
        try:
            self._require_page().keyboard.press(f"{_select_all_modifier()}+a")
        except Exception as exc:  # noqa: BLE001
            return f"failed: {exc}"
        return "OK select_all"

    def scroll(
        self,
        direction: str,
        amount: int = 5,
        x: float = 159,
        y: float = 350,
    ) -> str:
        """Wheel-scroll the page (ScrollableDevice). Pixels = amount * per-amount.

        down=+y, up=-y, right=+x, left=-x. Moves the pointer to (x,y) first so the
        wheel lands on the intended scroll container.
        """
        page = self._require_page()
        dist = max(1, int(amount)) * _SCROLL_PX_PER_AMOUNT
        d = (direction or "").strip().lower()
        dx = dy = 0
        if d in ("down", "向下", "downward"):
            dy = dist
        elif d in ("up", "向上", "upward"):
            dy = -dist
        elif d in ("right", "向右", "rightward"):
            dx = dist
        elif d in ("left", "向左", "leftward"):
            dx = -dist
        else:
            return f"failed: unknown scroll direction {direction!r}"
        try:
            page.mouse.move(x, y)
            page.mouse.wheel(dx, dy)
        except Exception as exc:  # noqa: BLE001
            return f"failed: {exc}"
        return f"OK scroll {d} {dist}px @({x:.0f},{y:.0f})"

    def drag(
        self,
        from_x: float,
        from_y: float,
        to_x: float,
        to_y: float,
        duration_ms: int = 1000,
        cursor_mode: str | None = None,
    ) -> str:
        """Pointer drag (mouse down -> stepped move -> up). ``cursor_mode`` ignored.

        Stepped intermediate moves let drag-driven widgets (sliders, canvases)
        register a continuous gesture rather than a teleport.
        """
        page = self._require_page()
        steps = max(1, min(int(duration_ms / 16) if duration_ms else 10, 60))
        try:
            page.mouse.move(from_x, from_y)
            page.mouse.down()
            for i in range(1, steps + 1):
                ix = from_x + (to_x - from_x) * i / steps
                iy = from_y + (to_y - from_y) * i / steps
                page.mouse.move(ix, iy)
            page.mouse.up()
        except Exception as exc:  # noqa: BLE001
            return f"failed: {exc}"
        return f"OK drag ({from_x:.0f},{from_y:.0f})->({to_x:.0f},{to_y:.0f})"

    def press_home(self) -> str:
        """Browser "go to root": navigate to the configured ``start_url`` if set,
        else a no-op (browser has no iOS home screen)."""
        if self.start_url:
            return self.navigate(self.start_url)
        return "OK home noop (no start_url configured)"

    # ----- browser-only extras --------------------------------------------
    def navigate(self, url: str) -> str:
        try:
            self._require_page().goto(url)
        except Exception as exc:  # noqa: BLE001
            return f"failed: {exc}"
        return f"OK navigate {url}"

    def go_back(self) -> str:
        try:
            self._require_page().go_back()
        except Exception as exc:  # noqa: BLE001
            return f"failed: {exc}"
        return "OK back"

    # ----- internals -------------------------------------------------------
    def _require_page(self):
        if self.page is None:
            raise RuntimeError("PlaywrightDevice 尚未连接（先调用 connect()）")
        return self.page


def _select_all_modifier() -> str:
    """Select-all modifier: Meta on macOS, Control elsewhere."""
    import sys

    return "Meta" if sys.platform == "darwin" else "Control"
