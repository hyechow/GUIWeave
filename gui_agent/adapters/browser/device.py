"""Playwright/CDP I/O backend implementing gui_agent.core.contracts.Device.

``PlaywrightDevice`` attaches to a user's running Chrome over the Chrome DevTools
Protocol (CDP) and drives the active page through a desktop pointer + keyboard. It
satisfies the neutral ``Device`` Protocol (connect/close/screenshot/tap/type_text/
drag/press_home) AND the optional ``ScrollableDevice`` capability (scroll), plus
the browser-only extras ``navigate(url)`` / ``go_back()``.

UNLIKE mobile (one screen forever), the web has N tabs and a click / ``window.open``
can spawn a NEW tab (often in the background). ``self.page`` is bound once at
``connect()``; left alone it would keep screenshotting + clicking the old tab, so
every observe/input first calls ``_follow_active_tab()`` which snapshots the page
set and, when a tab appears that wasn't there before, follows the newcomer (see
there for why foreground/``visibilityState`` is the wrong signal here). This keeps
core's "observation surface is a single screen" abstraction intact while the
adapter quietly follows the tab a click opened.

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
        self._prev_pages = None  # last-seen page list, for new-tab-appearance follow
        self._prev_raw_ids = None  # last-seen raw-CDP page-target id set (always fresh)
        self._browser_cdp = None  # browser-level CDP session for Target.getTargets

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
        # Initial bind; _follow_active_tab() re-points this at any tab a click later
        # spawns (see there). Reset the new-tab snapshot for a fresh connection.
        self.page = pages[0] if pages else self._context.new_page()
        self._prev_pages = None
        self._prev_raw_ids = None
        self._browser_cdp = None
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
                self._prev_pages = None
                self._prev_raw_ids = None
                self._browser_cdp = None

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
        # Follow the foreground tab first: a click on the previous turn may have
        # opened a new front tab, and we must observe THAT, not the stale one.
        self._follow_active_tab()
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
        self._follow_active_tab()
        try:
            self._require_page().mouse.click(x, y)
        except Exception as exc:  # noqa: BLE001 — surface as status str for executor
            return f"failed: {exc}"
        return f"OK tap ({x:.0f},{y:.0f})"

    def type_text(self, text: str) -> str:
        self._follow_active_tab()
        try:
            self._require_page().keyboard.type(text)
        except Exception as exc:  # noqa: BLE001
            return f"failed: {exc}"
        return f"OK type {text!r}"

    def press_enter(self) -> str:
        self._follow_active_tab()
        try:
            self._require_page().keyboard.press("Enter")
        except Exception as exc:  # noqa: BLE001
            return f"failed: {exc}"
        return "OK enter"

    def clear_text(self) -> str:
        """Select-all + delete in the focused field (browser select-all is Meta/Ctrl-A)."""
        self._follow_active_tab()
        try:
            page = self._require_page()
            page.keyboard.press(f"{_select_all_modifier()}+a")
            page.keyboard.press("Delete")
        except Exception as exc:  # noqa: BLE001
            return f"failed: {exc}"
        return "OK clear"

    def select_all(self) -> str:
        """Select all text in the focused field (used before re-typing)."""
        self._follow_active_tab()
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
        self._follow_active_tab()
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
        self._follow_active_tab()
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

    # ----- auth / cookies (raw CDP — replaces headless ui_login) -----------
    # The example WebArena human agent spins up a HEADLESS browser purely to log
    # in and dump a Playwright ``storage_state``. We don't need it: the session is
    # just cookies, and CDP reads/writes them natively. ``load_cookies`` replays a
    # saved state into the live takeover tab; ``dump_cookies`` mints one FROM the
    # live tab (so you log in once — manually in the persistent profile or via the
    # agent — then capture it here, with no headless browser anywhere).

    @staticmethod
    def _storage_state_cookies(storage_state) -> list[dict]:
        """Accept a path / JSON str / parsed dict / raw cookie list and return the
        Playwright-format cookie dicts (``{name,value,domain,path,...}``)."""
        import json

        data = storage_state
        if isinstance(data, (str, bytes)):
            text = data.decode() if isinstance(data, bytes) else data
            # A filesystem path vs. an inline JSON blob.
            if text.lstrip()[:1] in "[{":
                data = json.loads(text)
            else:
                with open(text) as fh:
                    data = json.load(fh)
        if isinstance(data, dict):
            return list(data.get("cookies") or [])
        if isinstance(data, list):
            return list(data)
        return []

    @staticmethod
    def _to_cdp_cookie(c: dict) -> dict:
        """Map one Playwright/storage_state cookie to a CDP ``Network.CookieParam``."""
        out: dict = {"name": c["name"], "value": c["value"]}
        # Anchor by domain+path when present; else fall back to a url so CDP can
        # infer scope. Strip a leading dot — CDP setCookie wants a bare host.
        domain = c.get("domain")
        if domain:
            out["domain"] = domain.lstrip(".")
        out["path"] = c.get("path") or "/"
        if c.get("secure") is not None:
            out["secure"] = bool(c["secure"])
        if c.get("httpOnly") is not None:
            out["httpOnly"] = bool(c["httpOnly"])
        ss = c.get("sameSite")
        if ss in ("Strict", "Lax", "None"):
            out["sameSite"] = ss
        exp = c.get("expires")
        # Playwright uses -1 for a session cookie; CDP treats "no expires" as session.
        if isinstance(exp, (int, float)) and exp > 0:
            out["expires"] = exp
        return out

    def load_cookies(self, storage_state, *, then_navigate: str | None = None) -> str:
        """Inject saved session cookies into the attached Chrome over raw CDP.

        ``storage_state`` may be a path to a Playwright ``storage_state.json``, an
        inline JSON string, a parsed dict, or a bare cookie list. Cookies are pushed
        via ``Network.setCookies`` (batch; falls back to per-cookie ``Network.setCookie``
        so one malformed entry can't drop the rest). Pass ``then_navigate`` (e.g. the
        task start_url) to reload into the now-authenticated page. Returns a status
        str the caller can scan; never raises.
        """
        try:
            raw = self._storage_state_cookies(storage_state)
        except Exception as exc:  # noqa: BLE001
            return f"failed: cannot read storage_state ({exc})"
        if not raw:
            return "failed: no cookies in storage_state"

        cdp_cookies = []
        for c in raw:
            try:
                cdp_cookies.append(self._to_cdp_cookie(c))
            except Exception:  # noqa: BLE001 — skip malformed, keep the rest
                continue

        injected = 0
        try:
            self._cdp_send("Network.setCookies", {"cookies": cdp_cookies})
            injected = len(cdp_cookies)
        except Exception:
            # Batch rejected (often one bad sameSite/secure combo) — set individually.
            for ck in cdp_cookies:
                try:
                    self._cdp_send("Network.setCookie", ck)
                    injected += 1
                except Exception:  # noqa: BLE001
                    continue

        if injected == 0:
            return "failed: 0 cookies injected"

        nav = ""
        if then_navigate:
            nav = "  " + self.navigate(then_navigate)
        return f"OK load_cookies {injected}/{len(raw)} injected{nav}"

    def dump_cookies(self, path: str) -> str:
        """Export the attached Chrome's live cookies to a Playwright-format
        ``storage_state.json`` via raw CDP ``Network.getAllCookies`` — lets you
        capture a login state minted in the takeover browser itself (no headless).
        ``origins``/localStorage is left empty (auth lives in cookies). Returns a
        status str; never raises."""
        import json

        try:
            res = self._cdp_send("Network.getAllCookies", {})
        except Exception as exc:  # noqa: BLE001
            return f"failed: getAllCookies ({exc})"
        out_cookies = []
        for c in res.get("cookies") or []:
            out_cookies.append(
                {
                    "name": c.get("name"),
                    "value": c.get("value"),
                    "domain": c.get("domain"),
                    "path": c.get("path") or "/",
                    "expires": c.get("expires", -1),
                    "httpOnly": bool(c.get("httpOnly")),
                    "secure": bool(c.get("secure")),
                    "sameSite": c.get("sameSite") or "Lax",
                }
            )
        try:
            with open(path, "w") as fh:
                json.dump({"cookies": out_cookies, "origins": []}, fh, indent=2)
        except Exception as exc:  # noqa: BLE001
            return f"failed: write {path} ({exc})"
        return f"OK dump_cookies {len(out_cookies)} -> {path}"

    # ----- browser-only extras --------------------------------------------
    def navigate(self, url: str) -> str:
        try:
            self._require_page().goto(url)
        except Exception:
            # High-level goto is flaky on a broken connect_over_cdp binding
            # ("Frame has been detached" / hangs). Fall back to raw CDP, which is
            # the same regime screenshot/evaluate already rely on here.
            return self._cdp_navigate(url)
        return f"OK navigate {url}"

    def _cdp_navigate(self, url: str, *, timeout_s: float = 10.0) -> str:
        """Navigate via raw CDP ``Page.navigate`` and poll readyState — robust when
        the high-level ``page.goto`` detaches/hangs on a version-skewed CDP attach."""
        import time

        try:
            self._cdp_send("Page.enable", {})
            self._cdp_send("Page.navigate", {"url": url})
        except Exception as exc:  # noqa: BLE001
            return f"failed: {exc}"
        deadline = timeout_s / 0.25
        i = 0
        while i < deadline:
            try:
                r = self._cdp_send(
                    "Runtime.evaluate",
                    {"expression": "document.readyState", "returnByValue": True},
                )
                if (r.get("result") or {}).get("value") == "complete":
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.25)
            i += 1
        return f"OK navigate {url} (cdp)"

    def go_back(self) -> str:
        try:
            self._require_page().go_back()
        except Exception as exc:  # noqa: BLE001
            return f"failed: {exc}"
        return "OK back"

    # ----- active-tab follow ----------------------------------------------
    def _follow_active_tab(self) -> None:
        """Re-point ``self.page`` at the tab a click / ``window.open`` just spawned.

        On the web a click can open a NEW tab while ``self.page`` stays pinned to the
        old one — that is exactly how "click landed, screen never changed" happens
        (mobile has one screen, so core never modeled this).

        We follow by **new-tab appearance**, NOT by foreground/``visibilityState``.
        An earlier version followed the 'visible' tab and kept missing this case:
        under a CDP-synthesized click the new tab frequently opens in the BACKGROUND
        (the old tab stays 'visible'), and under this adapter's occluded / non-preempt
        window visibility is unreliable for ALL tabs — so "follow the visible tab"
        either kept the wrong tab or found no foreground at all. Instead we snapshot
        the page set each call and, when a page appears that wasn't there before,
        follow the newest newcomer regardless of foreground.

        ⚠️ ``context.pages`` LAGS for tabs opened OUT OF BAND (by a click / window.open):
        sync-Playwright only ingests the ``Target.targetCreated`` event when one of its
        own calls yields to the dispatcher, and our screenshots go through raw CDP — so
        a bare screenshot/settle never pumps it and ``context.pages`` stays stale at the
        old count (measured: rawCDP=3 while context.pages=2 until a Playwright wait).
        Raw CDP ``Target.getTargets`` is always fresh, so we use it to NOTICE a changed
        tab set, then pump the dispatcher (``wait_for_timeout``) until context.pages
        catches up — we still need the Page object for high-level input. Best-effort —
        never raises, never recurses (no ``_require_page`` / ``_cdp_send``).
        """
        ctx = self._context
        if ctx is None:
            return
        # Detect a tab-set change via always-fresh raw CDP; only then pump the (sync)
        # event loop so context.pages absorbs an out-of-band (click-opened) tab.
        raw_ids = self._raw_page_target_ids()
        if raw_ids is not None and raw_ids != self._prev_raw_ids:
            self._prev_raw_ids = raw_ids
            self._pump_until_pages(len(raw_ids))
        try:
            pages = [p for p in ctx.pages if not _page_closed(p)]
        except Exception:
            return
        if not pages:
            return
        prev = self._prev_pages
        self._prev_pages = pages  # snapshot for the next call
        if prev is None:
            # First observation: keep connect()'s choice; just seed the snapshot.
            if self.page not in pages:
                self._switch_page(pages[-1])
            return
        # A page we hadn't seen before = a tab a click/window.open just opened. Follow
        # the newest such tab even if it opened in the background — that is where the
        # action led. (Same-tab navigations add no page, so they never trigger this.)
        newcomers = [p for p in pages if p not in prev]
        if newcomers:
            target = newcomers[-1]
            self._switch_page(target)
            print(f"  [tab] 新标签页（{len(prev)}->{len(pages)}），跟随 -> {self._page_url(target)}")
            return
        # No new tab: stay put unless our current tab has gone away.
        if self.page not in pages:
            self._switch_page(pages[-1])
            print(f"  [tab] 标签页关闭（{len(prev)}->{len(pages)}），切换 -> {self._page_url(self.page)}")

    def _raw_page_target_ids(self):
        """The set of page-target ids from raw CDP ``Target.getTargets`` — ALWAYS
        fresh (unlike context.pages, which lags for out-of-band tabs). Uses a
        browser-level CDP session, created lazily. None on any failure."""
        try:
            if self._browser_cdp is None:
                self._browser_cdp = self._browser.new_browser_cdp_session()
            res = self._browser_cdp.send("Target.getTargets")
            return frozenset(
                t["targetId"]
                for t in (res.get("targetInfos") or [])
                if t.get("type") == "page"
            )
        except Exception:
            return None

    def _pump_until_pages(self, target_n: int, *, tries: int = 5, ms: int = 120) -> None:
        """Pump the sync-Playwright dispatcher until ``context.pages`` reaches
        ``target_n`` (the fresh raw-CDP count), so an out-of-band tab is surfaced as a
        Page. ``wait_for_timeout`` yields to the dispatcher (a bare sleep does not).
        Bounded; best-effort."""
        for _ in range(tries):
            try:
                if len(self._context.pages) >= target_n:
                    return
                self._require_page().wait_for_timeout(ms)
            except Exception:
                return

    def _switch_page(self, page) -> None:
        """Bind ``self.page`` to ``page`` and drop the stale per-page CDP session
        (``_cdp_send`` rebuilds it lazily against the new target)."""
        if page is not self.page:
            self.page = page
            self._cdp = None

    def _page_url(self, page) -> str:
        """Best-effort URL of ``page`` via raw CDP ``Target.getTargetInfo`` (page.url
        is '' under the broken binding). Diagnostics only; '' on any failure."""
        try:
            sess = self._context.new_cdp_session(page)
        except Exception:
            return ""
        try:
            info = sess.send("Target.getTargetInfo", {}) or {}
            return (info.get("targetInfo") or {}).get("url") or ""
        except Exception:
            return ""
        finally:
            try:
                sess.detach()
            except Exception:
                pass

    # ----- internals -------------------------------------------------------
    def _require_page(self):
        if self.page is None:
            raise RuntimeError("PlaywrightDevice 尚未连接（先调用 connect()）")
        return self.page


def _select_all_modifier() -> str:
    """Select-all modifier: Meta on macOS, Control elsewhere."""
    import sys

    return "Meta" if sys.platform == "darwin" else "Control"


def _page_closed(page) -> bool:
    """Whether a Playwright page has been closed (treated as closed on error)."""
    try:
        return page.is_closed()
    except Exception:
        return True
