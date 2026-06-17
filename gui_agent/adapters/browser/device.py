"""Playwright/CDP I/O backend implementing gui_agent.core.runtime.contracts.Device.

``PlaywrightDevice`` attaches to a running Chrome for Testing over the Chrome
DevTools Protocol (CDP) and drives the active page through a desktop pointer +
keyboard.  It satisfies the neutral ``Device`` Protocol (connect/close/screenshot/
tap/type_text/drag/press_home) AND the optional ``ScrollableDevice`` capability
(scroll), plus the browser-only extras ``navigate(url)`` / ``go_back()``.

UNLIKE mobile (one screen forever), the web has N tabs and a click / window.open
can spawn a NEW tab (often in the background).  ``self.page`` is bound once at
``connect()``; every observe/input first calls ``_follow_active_tab()`` which
snapshots context.pages and, when a tab appears that wasn't there before, follows
the newcomer.  This keeps core's "single screen" abstraction intact.

Coordinates passed to ``tap`` / ``drag`` / ``scroll`` are VIEWPORT PIXELS — the
executor denormalizes 0-1000 -> px using ``viewport_size`` before calling here.

Every input method returns a status ``str`` (the executor scans it for
"paused" / "interrupted" / "failed"). ``close()`` detaches from Chrome WITHOUT
closing the user's browser.
"""

from __future__ import annotations

import json
import os
import signal
import threading
import time
from typing import Optional

# Default viewport used to denormalize coordinates and for screenshots when the
# real page viewport cannot be read (CDP pages frequently report viewport_size as
# None for the user's existing tab). Typical laptop content size.
_DEFAULT_VIEWPORT_W = 1280
_DEFAULT_VIEWPORT_H = 800

# Hard wall-clock cap for a single raw-CDP send. A non-responding Chrome would
# otherwise hang the agent loop forever. Normal round-trips are well under a second.
_CDP_SEND_TIMEOUT_S = 10.0

# CDP settle (wait_settled): the page is "settled" once it is at least interactive AND both
# the DOM and the network have been quiet for _SETTLE_QUIET_MS. This reads the page's real
# load/DOM/network state instead of diffing pixels, so a canvas/requestAnimationFrame render
# loop (which never touches the DOM or the network) no longer keeps the agent waiting — the
# thing pure-vision settle can't distinguish from "still loading". Network quiet = no NEW
# request for the window (tolerates persistent WebSocket/SSE connections that never "finish",
# which a naive in-flight==0 count would wait on forever). Measured on the live RoboTeam map:
# ~1.1s vs the vision loop's ~5.7s on the same transition.
_SETTLE_QUIET_MS = 400.0     # DOM-mutation / network quiet window
_SETTLE_POLL_S = 0.12        # poll cadence (raw-CDP evaluate is cheap)
# Hard cap. settle only waits for THIS action's immediate effect to land — it is NOT the
# mechanism for waiting out a long page load (a big SPA like Feishu can take far longer than any
# sane cap). That is handled by re-observing each turn: a page still loading reads as in_progress
# and the next turn sees it further along. So keep the cap short; a page still busy at the cap
# returns anyway (the log says 仍在加载) and the loop re-checks next turn.
_SETTLE_CAP_S = 3.0
# An XHR/Fetch in flight longer than this is treated as streaming / long-poll / an unconsumed
# body (it may never fire loadingFinished) and excluded from the gate — otherwise such a page
# would settle at the cap on every action. Kept under _SETTLE_CAP_S so it excludes a stuck
# request and returns 'settled' BEFORE the cap. A genuinely slow one-shot fetch is waited on up
# to here; past it the next turn's observe picks up the late data (the same re-observe path that
# handles long loads).
_XHR_STALE_S = 2.0

# A MutationObserver storing the time of the last DOM change on window.__q.t (installed once
# per document, guarded; re-installs after a navigation wipes window.__q). _SETTLE_RESET sets
# the baseline to now (called on wait_settled entry, so quiet is measured FROM THE ACTION);
# _SETTLE_PROBE returns [readyState, msSinceLastMutation] in one round-trip.
_SETTLE_INSTALL = (
    "if(!window.__q){window.__q={t:performance.now()};"
    "new MutationObserver(()=>{window.__q.t=performance.now();}).observe(document.documentElement||document,"
    "{subtree:true,childList:true,attributes:true,characterData:true});}"
)
_SETTLE_RESET = "(()=>{" + _SETTLE_INSTALL + "window.__q.t=performance.now();return 1;})()"
_SETTLE_PROBE = (
    "(()=>{" + _SETTLE_INSTALL
    + "return [document.readyState, Math.round(performance.now()-window.__q.t)];})()"
)


class _CDPTimeout(Exception):
    """A raw-CDP send exceeded _CDP_SEND_TIMEOUT_S (Chrome did not respond)."""


def _cdp_alarm(signum, frame):  # SIGALRM handler — interrupts the blocked send
    raise _CDPTimeout()

# Pixels of wheel scroll per unit of the iphone-style ``amount`` (amount=5 -> a
# bit under one screen). Keeps the neutral scroll(direction, amount=5, ...)
# signature identical to ScrollableDevice while mapping to real wheel deltas.
_SCROLL_PX_PER_AMOUNT = 120


class PlaywrightDevice:
    """Desktop-pointer web device over CDP with optional read-only DOM sensors."""

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
        self._cdp = None  # cached raw CDP session (for window_bounds / eval_js fallback)
        self._browser_cdp = None  # cached browser-level CDP session (Target.* for tabs)
        self._prev_pages = None  # last-seen page list, for new-tab-appearance follow
        self._tab_switched = False  # set by _switch_page; cleared by pop_tab_switched()
        self._last_viewport = None  # CSS-px (w, h) derived from the last screenshot
        self._dpr = None  # cached devicePixelRatio (stable; only changes across monitors)
        self._pending_upload = None  # file path armed for the NEXT file chooser (upload action)
        self._upload_result = None   # set by the file-chooser handler so upload_file can report
        # CDP settle network tracking (armed lazily on the live CDP session, re-armed if it is
        # rebuilt). Tracks in-flight XHR/Fetch ONLY — the data the page is waiting on — so a slow
        # post-readyState fetch blocks settle, while persistent Script/blob workers and SSE (which
        # fire once and never "finish") don't keep us waiting. _xhr_last = monotonic time of the
        # last XHR/Fetch start-or-finish (network-quiet = 0 in-flight AND quiet for the window).
        self._net_session = None
        self._xhr_ids: dict = {}    # requestId -> monotonic start time (in-flight XHR/Fetch)
        self._xhr_last = 0.0        # last XHR/Fetch start-or-finish (for the quiet window)

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
        pages = self._all_pages()
        # Initial bind: prefer Chrome CDP's /json/list front page. Playwright's
        # context.pages order is not the Chrome tab selection order, so pages[0] can
        # be a stale background tab.
        self.page = self._active_page_from_json_list(pages) or (pages[0] if pages else self._context.new_page())
        self._context = self.page.context
        self._arm_file_chooser(self.page)
        self._prev_pages = pages if pages else [self.page]
        self._tab_switched = False
        self._last_viewport = None
        self._dpr = None
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
                self._browser_cdp = None
                self._prev_pages = None
                self._tab_switched = False
                self._last_viewport = None
                self._dpr = None

    # ----- viewport (for the executor's denormalization) -------------------
    @property
    def viewport_size(self) -> tuple[int, int]:
        """(width, height) in CSS px the executor denormalizes 0-1000 -> px against.

        DERIVED FROM THE SCREENSHOT, not a separate JS read. The PNG the LLM looked
        at IS the coordinate frame; its pixel size / devicePixelRatio is the CSS-px
        viewport ``page.mouse.click`` expects. Binding denorm to the captured image
        this way means it can NEVER desync from what the model saw.

        This replaces an earlier per-tap ``window.innerHeight`` read that could
        transiently fail and silently fall back to a hardcoded ``(1280, 800)``;
        when the real window was 1281x963 that scaled every y by 800/963 and lifted
        a left-nav tap ~40px up onto the wrong item (the "点云文档却开视频会议" bug).
        Now there is no separate, separately-failing size read at all.

        ``screenshot()`` refreshes this each turn. If queried before any screenshot
        (rare), fall back to a direct CSS-viewport read via CDP, then the default.
        """
        if self._last_viewport is not None:
            return self._last_viewport
        wh = self._read_css_viewport()
        if wh is not None:
            self._last_viewport = wh
            return wh
        return _DEFAULT_VIEWPORT_W, _DEFAULT_VIEWPORT_H

    def _device_pixel_ratio(self) -> float:
        """devicePixelRatio, cached. Stable for a session (only changes if the
        window moves to a differently-scaled monitor), so — unlike the viewport —
        it is safe to read ONCE. Raw CDP; defaults to 1.0 until a read succeeds."""
        if self._dpr is not None:
            return self._dpr
        try:
            res = self._cdp_send(
                "Runtime.evaluate",
                {"expression": "window.devicePixelRatio", "returnByValue": True},
            )
            dpr = float((res.get("result") or {}).get("value") or 0)
            if dpr > 0:
                self._dpr = dpr
                return dpr
        except Exception:
            pass
        return 1.0

    def _css_viewport_from_png(self, png: bytes) -> tuple[int, int] | None:
        """CSS-px viewport = PNG device-px size / devicePixelRatio. None if the PNG
        header can't be parsed."""
        size = _png_size(png)
        if size is None:
            return None
        dpr = self._device_pixel_ratio() or 1.0
        w, h = size
        return max(1, round(w / dpr)), max(1, round(h / dpr))

    def _read_css_viewport(self) -> tuple[int, int] | None:
        """Direct CSS-px viewport via raw CDP ``Page.getLayoutMetrics`` (no JS
        execution context needed). Only used as the before-first-screenshot
        fallback for ``viewport_size``."""
        try:
            m = self._cdp_send("Page.getLayoutMetrics", {})
            vp = m.get("cssVisualViewport") or m.get("cssLayoutViewport") or {}
            w = int(vp.get("clientWidth") or 0)
            h = int(vp.get("clientHeight") or 0)
            if w and h:
                return w, h
        except Exception:
            pass
        return None

    # ----- perception ------------------------------------------------------
    def screenshot(self) -> bytes:
        """Return the current page as PNG bytes, and refresh the denorm viewport.

        Primary path: page.screenshot() — fast, no navigation wait.
        Fallback: raw CDP Page.captureScreenshot (e.g. occluded window).

        After capture, derive the CSS-px viewport from THIS PNG's size /
        devicePixelRatio and cache it (see viewport_size), so the executor
        denormalizes 0-1000 against the exact frame the LLM is about to see —
        with no separate, separately-failing window.innerHeight read.
        """
        self._follow_active_tab()
        page = self._require_page()
        try:
            png = page.screenshot(type="png")
        except Exception:
            png = self._cdp_screenshot()
        wh = self._css_viewport_from_png(png)
        if wh is not None:
            self._last_viewport = wh
        return png

    def _cdp_send(self, method: str, params: dict) -> dict:
        """Send a raw CDP command on the cached per-page session, rebuilding it once
        if it went stale (the page target changed). Raw CDP is used throughout for
        anything the high-level Playwright page API would route through the
        execution-context binding (evaluate / wrapper screenshot / viewport), which
        hangs when that binding fails over ``connect_over_cdp`` on a mismatched
        Chrome — whereas raw CDP keeps working."""
        page = self._require_page()
        if self._cdp is None:
            self._cdp = page.context.new_cdp_session(page)
        try:
            return self._timed_send(method, params)
        except _CDPTimeout:
            raise  # don't retry a hang into another hang — let the caller fall back
        except Exception:
            self._cdp = page.context.new_cdp_session(page)
            return self._timed_send(method, params)

    def _timed_send(self, method: str, params: dict) -> dict:
        """``self._cdp.send`` with a hard wall-clock cap via SIGALRM, so a Chrome that
        never answers (intermittent ``Page.captureScreenshot`` stalls) raises
        ``_CDPTimeout`` instead of blocking ``selector.select`` forever. MAIN THREAD
        ONLY (SIGALRM); off it (or where SIGALRM is absent) it sends uncapped. The
        normal path adds only a setitimer arm/disarm — microseconds."""
        return self._timed_cdp_send(self._cdp, method, params)

    def _timed_cdp_send(self, session, method: str, params: dict) -> dict:
        """Send on a CDP session with the same SIGALRM cap used by _cdp_send."""
        if (
            threading.current_thread() is not threading.main_thread()
            or not hasattr(signal, "SIGALRM")
        ):
            return session.send(method, params)
        prev = signal.signal(signal.SIGALRM, _cdp_alarm)
        signal.setitimer(signal.ITIMER_REAL, _CDP_SEND_TIMEOUT_S)
        try:
            return session.send(method, params)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, prev)

    def _cdp_screenshot(self) -> bytes:
        import base64

        result = self._cdp_send("Page.captureScreenshot", {"format": "png"})
        return base64.b64decode(result["data"])

    def is_loading(self) -> bool:
        """True only when document.readyState == 'loading' (structural sensor).

        SPAs like Feishu stay at 'interactive' for a long time — only 'loading'
        means the page is truly not yet actionable.  False on any failure.
        """
        try:
            res = self._cdp_send(
                "Runtime.evaluate",
                {"expression": "document.readyState", "returnByValue": True},
            )
            return (res.get("result", {}) or {}).get("value") == "loading"
        except Exception:
            return False

    # ----- CDP settle (replaces pixel-diff settle for the browser) ----------
    def _ensure_net_tracking(self) -> None:
        """Arm in-flight XHR/Fetch tracking on the live CDP session, re-arming if _cdp_send
        rebuilt it (the listener is bound to a session object). Only XHR/Fetch count — Script /
        Document / Image / EventSource don't (workers/SSE fire once and never finish, which would
        wedge the gate; the initial document load is covered by readyState + DOM-quiet instead).
        Best-effort; on failure the net gate degrades to 'quiet' and wait_settled leans on
        DOM/readyState."""
        if self._cdp is None:
            try:
                self._cdp_send("Runtime.evaluate", {"expression": "0", "returnByValue": True})
            except Exception:
                return
        if self._cdp is None or self._net_session is self._cdp:
            return
        try:
            self._cdp.send("Network.enable", {})

            def _on_req(p):
                if p.get("type") in ("XHR", "Fetch"):
                    self._xhr_ids[p.get("requestId")] = time.monotonic()
                    self._xhr_last = time.monotonic()

            def _on_done(p):
                if self._xhr_ids.pop(p.get("requestId"), None) is not None:
                    self._xhr_last = time.monotonic()

            self._cdp.on("Network.requestWillBeSent", _on_req)
            self._cdp.on("Network.loadingFinished", _on_done)
            self._cdp.on("Network.loadingFailed", _on_done)
            self._net_session = self._cdp
        except Exception:
            self._net_session = None

    def wait_settled(self, action_type: Optional[str] = None) -> tuple[float, bool]:
        """Wait until the page is settled using CDP signals (not pixel diffing). Returns
        (seconds_waited, no_effect).

        Settled = readyState != 'loading' AND the DOM has not mutated for _SETTLE_QUIET_MS AND
        no NEW network request for _SETTLE_QUIET_MS, capped at _SETTLE_CAP_S. Both quiet windows
        are measured FROM THE ACTION (baselines reset on entry), so a just-fired effect is never
        mistaken for pre-existing quiet. A canvas/rAF render loop never mutates the DOM or the
        network, so it can't keep us waiting — the case pure-vision settle can't escape.

        no_effect = the DOM never mutated during the wait (quietMs tracked total elapsed), i.e.
        the action changed nothing structurally. The supervisor corroborates / corrects this
        with its url + dom-fingerprint signals. Raises on CDP failure so the caller falls back
        to the vision settle."""
        self._ensure_net_tracking()
        self._cdp_send("Runtime.evaluate", {"expression": _SETTLE_RESET, "returnByValue": True})
        t0 = time.perf_counter()
        while True:
            res = self._cdp_send("Runtime.evaluate", {"expression": _SETTLE_PROBE, "returnByValue": True})
            val = res.get("result", {}).get("value")
            rs, q = (val[0], val[1]) if isinstance(val, list) and len(val) == 2 else ("complete", None)
            # network quiet = no (non-stale) XHR/Fetch in flight AND none started/finished in the
            # window. Drop requests stuck in flight > _XHR_STALE_S (streaming / unconsumed body).
            now = time.monotonic()
            self._xhr_ids = {i: t for i, t in self._xhr_ids.items() if now - t < _XHR_STALE_S}
            xhr_inflight = len(self._xhr_ids)
            net_quiet = xhr_inflight == 0 and (now - self._xhr_last) * 1000.0 >= _SETTLE_QUIET_MS
            elapsed = time.perf_counter() - t0
            settled = rs != "loading" and (q or 0) >= _SETTLE_QUIET_MS and net_quiet
            if settled or elapsed >= _SETTLE_CAP_S:
                # DOM never mutated after entry → quietMs climbed with elapsed (≈ equal).
                no_effect = q is not None and q >= elapsed * 1000.0 - 150 and xhr_inflight == 0
                tag = "settled" if settled else "达上限·仍在加载"
                print(f"  [Settle] {elapsed:.1f}s (CDP {tag}: readyState={rs}, "
                      f"domQuiet={q}ms, xhr在飞={xhr_inflight}"
                      + ("，零效果" if no_effect else "") + ")")
                return elapsed, no_effect
            self._ensure_net_tracking()  # re-arm if a poll rebuilt the session
            time.sleep(_SETTLE_POLL_S)

    def page_info(self) -> tuple[str, str]:
        """(url, title) of the active page — the browser chrome the vision-only screenshot
        omits, so the checker can judge page identity from ground truth instead of fabricating.

        Via raw CDP ``Target.getTargetInfo`` (returns both, no JS execution) — robust to the
        high-level ``page.url`` / ``page.evaluate`` breakage over connect_over_cdp on a
        mismatched Chrome. Falls back to ``Runtime.evaluate`` if the target info lacks a url.
        ``("", "")`` on any failure; never raises.
        """
        try:
            info = self._cdp_send("Target.getTargetInfo", {}).get("targetInfo", {})
            url, title = info.get("url", "") or "", info.get("title", "") or ""
            if not url or not title:  # 任一缺就回退 JS，分别只补缺失的字段
                res = self._cdp_send(
                    "Runtime.evaluate",
                    {"expression": "[location.href, document.title]", "returnByValue": True},
                )
                pair = ((res.get("result", {}) or {}).get("value") or ["", ""]) + ["", ""]
                url = url or (pair[0] or "")
                title = title or (pair[1] or "")
            return url, title
        except Exception:
            return "", ""

    def form_state_fingerprint(self) -> str | None:
        """Short hash of the page's interactive state: every form control's value/checked
        plus the focused element. The structural progress signal behind Observation.dom_state —
        filling a form changes this every turn while pixels stay near-identical and the
        planner's instructions read alike ("在X输入框输入Y"), so the stuck/repetition
        detectors get ground truth instead of guessing from text similarity. Raw CDP
        (page.evaluate is broken over connect_over_cdp here). None on any failure."""
        js = (
            "(()=>{const els=[...document.querySelectorAll('input,select,textarea')];"
            "const vals=els.map(e=>(e.type==='checkbox'||e.type==='radio')?(e.checked?'1':'0')"
            ":String(e.value??''));"
            "const a=document.activeElement;"
            "const focus=a?(a.tagName+':'+(a.name||a.id||a.placeholder||'')):'';"
            "return els.length+'|'+focus+'|'+vals.join('\\u0001');})()"
        )
        try:
            res = self._cdp_send("Runtime.evaluate", {"expression": js, "returnByValue": True})
            val = (res.get("result", {}) or {}).get("value")
        except Exception:
            return None
        if not isinstance(val, str) or not val:
            return None
        import hashlib

        return hashlib.md5(val.encode("utf-8")).hexdigest()[:16]

    def read_tables(self) -> list[dict]:
        """Return structured DOM table/grid snapshots from the current page.

        This is a read-only perception sensor. It does not click, scroll, export,
        or download; it only serializes rows already present in the live DOM.
        """
        from gui_agent.adapters.browser.table_reader import (
            normalize_table_snapshots,
            table_snapshot_js,
        )

        try:
            self._follow_active_tab()
            res = self._cdp_send(
                "Runtime.evaluate",
                {
                    "expression": table_snapshot_js(),
                    "returnByValue": True,
                    "awaitPromise": True,
                },
            )
            val = (res.get("result", {}) or {}).get("value")
            raw = json.loads(val) if isinstance(val, str) else val
            return normalize_table_snapshots(raw)
        except Exception:
            return []

    def read_complete_tables(self) -> list[dict]:
        """Return complete structured grid snapshots when a read-only provider exists.

        Magento Admin grids expose a same-origin JSON provider with pagination
        metadata. This method fetches those pages from inside the authenticated
        browser context, then falls back to ordinary DOM snapshots when no such
        provider is available.
        """
        from gui_agent.adapters.browser.table_reader import (
            complete_table_snapshot_js,
            normalize_table_snapshots,
        )

        try:
            self._follow_active_tab()
            res = self._cdp_send(
                "Runtime.evaluate",
                {
                    "expression": complete_table_snapshot_js(),
                    "returnByValue": True,
                    "awaitPromise": True,
                },
            )
            val = (res.get("result", {}) or {}).get("value")
            raw = json.loads(val) if isinstance(val, str) else val
            tables = normalize_table_snapshots(raw)
            return tables or self.read_tables()
        except Exception:
            return self.read_tables()

    def eval_js(self, expression: str) -> object:
        """Best-effort JS eval via page.evaluate(). Used by the action visualizer.
        Returns None on any failure; never raises."""
        try:
            return self._require_page().evaluate(expression)
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
            page = self._require_page()
            try:
                page.bring_to_front()
            except Exception:
                pass
            page.mouse.click(x, y)
        except Exception as exc:  # noqa: BLE001
            return f"failed: {exc}"
        return f"OK tap ({x:.0f},{y:.0f})"

    def dom_snap(
        self, x: float, y: float, target_text: str = ""
    ) -> "tuple[float, float, Optional[str]]":
        """Snap a viewport-CSS click point to the center of the small clickable element under it
        — browser's coordinate correction, the DOM analogue of iphone's YOLO snap.

        TEXT RETARGET (the OCR-snap analogue): when ``target_text`` is given (the label quoted
        in the action description, e.g. 「操作」) and the element under the point carries a
        DIFFERENT label, search nearby (≤80px) for a clickable whose exact trimmed text equals
        the target and snap there instead. This rescues the adjacent-menu-item failure mode:
        two 28px-high items (操作/删除), the vision model's y is one row off, and the click
        lands on the DESTRUCTIVE neighbour (run 20260612_114219: 3× clicked 删除 aiming 操作).
        The radius keeps same-label elements elsewhere on the page (e.g. a table header) out.

        CONSERVATIVE otherwise: only snaps to a reasonably-sized clickable element (button /
        link / list row / menu item / checkbox …). Returns the point UNCHANGED for canvas /
        svg / video / sliders / huge containers-overlays / when nothing clickable is under it.
        Coords are CSS viewport px. Returns ``(sx, sy, info)`` — ``info`` is "tag WxH" when
        snapped (tag='text' for a text retarget), else None.
        """
        import json

        target_js = json.dumps(target_text or "")
        js = (
            "(()=>{const x=%d,y=%d,target=%s;"
            "const CLICK='a,button,input,select,textarea,label,[role=button],"
            "[role=option],[role=menuitem],[role=tab],[role=checkbox],[role=radio],[onclick],.cursor-pointer,li';"
            "const txt=e=>((e.innerText||e.value||'')+'').trim();"
            "let el=document.elementFromPoint(x,y);if(!el)return '';"
            "const n=el.closest&&el.closest(CLICK);"
            "if(target && (!n || txt(n)!==target)){"
            "  let best=null,bd=1e9;"
            "  for(const c of document.querySelectorAll(CLICK)){"
            "    if(txt(c)!==target)continue;"
            "    const r=c.getBoundingClientRect();"
            "    if(r.width<=0||r.height<=0)continue;"
            "    const cx=r.x+r.width/2,cy=r.y+r.height/2,dd=Math.hypot(cx-x,cy-y);"
            "    if(dd<bd){bd=dd;best={cx:Math.round(cx),cy:Math.round(cy),"
            "      w:Math.round(r.width),h:Math.round(r.height)};}}"
            "  if(best&&bd<=80)return JSON.stringify({...best,tag:'text'});"
            "}"
            "if(!n)return '';const tag=n.tagName.toLowerCase();"
            "const role=(n.getAttribute&&n.getAttribute('role'))||'';"
            "const itype=tag==='input'?((n.getAttribute('type')||'').toLowerCase()):'';"
            "if(['canvas','svg','video','html','body','main','form'].includes(tag))return '';"
            "if(['slider','scrollbar'].includes(role)||itype==='range')return '';"
            "const r=n.getBoundingClientRect(),vw=innerWidth,vh=innerHeight;"
            "if(r.width<=0||r.height<=0||r.width>vw*0.9||r.height>vh*0.6)return '';"
            "return JSON.stringify({cx:Math.round(r.x+r.width/2),cy:Math.round(r.y+r.height/2),"
            "tag,w:Math.round(r.width),h:Math.round(r.height)});})()"
            % (int(round(x)), int(round(y)), target_js)
        )
        try:
            res = self._cdp_send("Runtime.evaluate", {"expression": js, "returnByValue": True})
            val = (res.get("result", {}) or {}).get("value")
        except Exception:  # noqa: BLE001
            return x, y, None
        if not val:
            return x, y, None
        try:
            d = json.loads(val)
            return float(d["cx"]), float(d["cy"]), f"{d['tag']} {d['w']}x{d['h']}"
        except Exception:  # noqa: BLE001
            return x, y, None

    def upload_file(self, x: float, y: float, file_path: str) -> str:
        """Click the upload control at (x,y) and feed ``file_path`` through the file chooser —
        WITHOUT the native dialog. Arms ``_pending_upload`` so the persistent ``_on_file_chooser``
        handler (see ``_arm_file_chooser``) delivers the file when the click opens the chooser.

        Works even when the page has no persistent ``<input type=file>`` (custom uploaders create
        an ephemeral input on click) because the chooser is keyed to the click, not a located
        node. Proven on RoboTeam 地图导入. (page.mouse / file chooser keep working over
        connect_over_cdp even though page.evaluate is bound-broken here.)
        """
        import os

        path = os.path.expanduser(file_path)
        if not os.path.exists(path):
            return f"failed: 文件不存在 {path}"
        self._follow_active_tab()
        try:
            page = self._require_page()
            try:
                page.bring_to_front()
            except Exception:
                pass
            self._pending_upload = path
            self._upload_result = None
            page.mouse.click(x, y)
            for _ in range(20):  # pump events so the file-chooser handler can run (~≤3s)
                if self._upload_result is not None:
                    break
                page.wait_for_timeout(150)
        except Exception as exc:  # noqa: BLE001
            self._pending_upload = None
            return f"failed: {exc}"
        self._pending_upload = None
        return self._upload_result or "failed: 点击后未弹出文件选择器（该控件可能不是上传入口）"

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
            self._cdp_send("Page.navigate", {"url": url})
        except Exception as exc:  # noqa: BLE001
            return f"failed: {exc}"
        return f"OK navigate {url}"

    def go_back(self) -> str:
        try:
            self._require_page().go_back()
        except Exception as exc:  # noqa: BLE001
            return f"failed: {exc}"
        return "OK back"

    # ----- tab management (CDP Target.* + Playwright pages) -----------------
    def _all_pages(self) -> list:
        """All open page tabs across ALL browser contexts (connect_over_cdp may put
        click-opened tabs in a fresh context, not self._context)."""
        pages: list = []
        try:
            for ctx in self._browser.contexts:
                try:
                    pages.extend(p for p in ctx.pages if not _page_closed(p))
                except Exception:
                    pass
        except Exception:
            pass
        return pages

    def _active_page_from_json_list(self, pages: list):
        """Best-effort selected tab using Chrome's HTTP CDP target list.

        Empirically, ``/json/list`` keeps the active page target at the front when
        Page.bringToFront or a user tab switch occurs. CDP Runtime visibility/focus
        probes are not reliable here: attaching/evaluating through some CDP paths can
        make multiple tabs report visible/focused. So use the target-list ordering as
        the active-tab signal, then map it back to Playwright's page object by
        targetId (with URL only as a fallback).
        """
        try:
            import json
            from urllib.request import urlopen

            raw = urlopen(self.cdp_url.rstrip("/") + "/json/list", timeout=1.0).read()
            targets = json.loads(raw.decode("utf-8"))
        except Exception:
            return None
        active = next(
            (t for t in targets if t.get("type") == "page" and t.get("id")),
            None,
        )
        if not active:
            return None
        active_id = active.get("id") or ""
        active_url = active.get("url") or ""
        for page in pages:
            if _page_closed(page):
                continue
            try:
                info = self._page_target_info(page)
                if info.get("targetId") == active_id:
                    return page
                if active_url and info.get("url") == active_url:
                    return page
            except Exception:
                continue
        return None

    def _page_target_info(self, page) -> dict:
        """CDP Target.getTargetInfo for a Playwright page.

        Playwright's high-level ``page.url`` can be empty over connect_over_cdp in
        this environment, but the per-page CDP target info still exposes the real
        targetId/url/title. This is how we map /json/list's active target back to
        the page object used for screenshot/input.
        """
        session = None
        try:
            session = page.context.new_cdp_session(page)
            return (self._timed_cdp_send(session, "Target.getTargetInfo", {}).get("targetInfo") or {})
        finally:
            if session is not None:
                try:
                    session.detach()
                except Exception:
                    pass

    def _target_meta(self) -> dict:
        """url -> title for page targets via raw CDP ``Target.getTargets`` (reliable
        titles even when the high-level page.title() would hang on a version skew)."""
        meta: dict = {}
        try:
            if self._browser_cdp is None:
                self._browser_cdp = self._browser.new_browser_cdp_session()
            res = self._browser_cdp.send("Target.getTargets")
            for t in res.get("targetInfos", []):
                if t.get("type") == "page":
                    meta[t.get("url", "")] = t.get("title", "")
        except Exception:
            self._browser_cdp = None
        return meta

    def list_tabs(self) -> list[tuple[int, str, str]]:
        """(index, title, url) for every open tab. Titles via raw CDP."""
        meta = self._target_meta()
        out: list[tuple[int, str, str]] = []
        for i, p in enumerate(self._all_pages()):
            try:
                url = p.url
            except Exception:
                url = ""
            out.append((i, meta.get(url, ""), url))
        return out

    def new_tab(self, url: Optional[str] = None) -> str:
        """Open a new tab (optionally navigate to ``url``) and make it the active tab."""
        try:
            p = self._context.new_page()
            self._switch_page(p)
            if url:
                return "OK new_tab; " + self.navigate(url)
            try:
                p.bring_to_front()
            except Exception:
                pass
        except Exception as exc:  # noqa: BLE001
            return f"failed: {exc}"
        return "OK new_tab"

    def select_tab(self, match: str) -> str:
        """Switch to the tab whose title or url contains ``match`` (case-insensitive)."""
        m = (match or "").strip().lower()
        if not m:
            return "failed: select_tab 需要 tab 标题或 url 子串"
        meta = self._target_meta()
        for p in self._all_pages():
            try:
                url = p.url
            except Exception:
                url = ""
            title = meta.get(url, "")
            if m in title.lower() or m in url.lower():
                try:
                    p.bring_to_front()
                    self._context = p.context
                    self._switch_page(p)
                except Exception as exc:  # noqa: BLE001
                    return f"failed: {exc}"
                return f"OK select_tab {title[:30]!r} {url[:40]}"
        return f"failed: 没有标题/url 含 {match!r} 的标签页"

    def close_tab(self, match: Optional[str] = None) -> str:
        """Close the tab matching ``match`` (title/url substring), or the current tab
        when ``match`` is empty. Re-points self.page at a remaining tab."""
        target = None
        if match and match.strip():
            m = match.strip().lower()
            meta = self._target_meta()
            for p in self._all_pages():
                try:
                    url = p.url
                except Exception:
                    url = ""
                if m in meta.get(url, "").lower() or m in url.lower():
                    target = p
                    break
            if target is None:
                return f"failed: 没有含 {match!r} 的标签页"
        else:
            target = self.page
        try:
            target.close()
        except Exception as exc:  # noqa: BLE001
            return f"failed: {exc}"
        remaining = self._all_pages()
        if remaining:
            self._context = remaining[-1].context
            self._switch_page(remaining[-1])
        return "OK close_tab"

    # ----- active-tab follow ----------------------------------------------
    def _follow_active_tab(self) -> None:
        """Re-point self.page at the tab a click / window.open just opened.

        Checks ALL browser contexts — under connect_over_cdp, click-opened tabs
        may land in a new context rather than self._context, so we must iterate
        self._browser.contexts rather than only self._context.pages.

        WHY wait_for_timeout: Playwright's sync dispatcher only ingests
        Target.targetCreated (and updates context.pages) when one of its own
        calls yields to the event loop. time.sleep() does NOT do this. One pump
        on any page covers all contexts (shared transport under connect_over_cdp).
        """
        if self._browser is None:
            return
        # Flush pending Playwright events so context.pages lists are up-to-date.
        if self.page is not None:
            try:
                self.page.wait_for_timeout(200)
            except Exception:
                pass
        # Collect pages across ALL contexts — connect_over_cdp puts click-opened
        # tabs in a fresh context, not necessarily self._context.
        all_pages: list = []
        try:
            for ctx in self._browser.contexts:
                try:
                    all_pages.extend(p for p in ctx.pages if not _page_closed(p))
                except Exception:
                    pass
        except Exception:
            return
        if not all_pages:
            return
        prev = self._prev_pages
        self._prev_pages = all_pages
        if prev is None:
            if self.page not in all_pages:
                self._switch_page(all_pages[-1])
            return
        newcomers = [p for p in all_pages if p not in prev]
        if newcomers:
            target = newcomers[-1]
            self._context = target.context
            self._switch_page(target)
            print(f"  [tab] 新标签页（{len(prev)}->{len(all_pages)}），跟随 -> {target.url}")
            return
        if self.page not in all_pages:
            self._switch_page(all_pages[-1])
            print(f"  [tab] 标签页关闭，切换 -> {self.page.url}")

    def pop_tab_switched(self) -> bool:
        """Return True (and clear the flag) if a tab switch happened since the last
        call. Used by the settle loop to detect navigation-to-new-tab even when the
        new tab looks visually similar to the old one (below frame_analysis.frame_changed
        thresholds)."""
        val = self._tab_switched
        self._tab_switched = False
        return val

    def _switch_page(self, page) -> None:
        """Bind ``self.page`` to ``page`` and drop the stale per-page CDP session
        (``_cdp_send`` rebuilds it lazily against the new target)."""
        if page is not self.page:
            self.page = page
            self._cdp = None
            self._tab_switched = True
            self._arm_file_chooser(page)

    # ── File-chooser safety net ────────────────────────────────────────────
    def _arm_file_chooser(self, page) -> None:
        """Register a PERSISTENT file-chooser handler on ``page`` (once).

        Registering a 'filechooser' listener makes Playwright intercept EVERY file chooser, so
        the native OS dialog never opens — which would otherwise block the whole CDP transport
        and hang the agent. The LLM occasionally plain-taps a 选择文件/上传 control instead of
        emitting the upload action; this catches that. See ``_on_file_chooser``.
        """
        if page is None or getattr(page, "_fc_armed", False):
            return
        try:
            page.on("filechooser", self._on_file_chooser)
            page._fc_armed = True
        except Exception:  # noqa: BLE001
            pass

    def _on_file_chooser(self, chooser) -> None:
        """Route an intercepted file chooser: the armed upload file, else CANCEL it.

        ``upload_file`` arms ``_pending_upload`` right before clicking the upload control, so the
        next chooser gets that file. Any OTHER chooser (a stray tap that opened a file picker) is
        cancelled with an empty selection — the native dialog never blocks the agent.
        """
        import os

        path = self._pending_upload
        self._pending_upload = None
        if path:
            try:
                chooser.set_files(path)
                self._upload_result = f"OK upload {os.path.basename(path)}"
            except Exception as exc:  # noqa: BLE001
                self._upload_result = f"failed: {exc}"
        else:
            try:
                chooser.set_files([])  # empty = cancel; no file selected
            except Exception:  # noqa: BLE001
                pass
            print("  [FileChooser] 拦截并取消了一个非预期的文件选择弹窗（防原生框卡死）")

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


def _png_size(png: bytes) -> tuple[int, int] | None:
    """(width, height) in pixels from a PNG's IHDR header — no PIL/decode needed.
    The 8-byte signature is followed by the IHDR chunk whose width/height are two
    big-endian uint32 at byte offsets 16 and 20. None if it isn't a PNG."""
    import struct

    if len(png) < 24 or png[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    try:
        w, h = struct.unpack(">II", png[16:24])
    except Exception:
        return None
    return (int(w), int(h)) if w and h else None
