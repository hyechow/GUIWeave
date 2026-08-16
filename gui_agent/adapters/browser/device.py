"""Playwright/CDP I/O backend implementing gui_agent.core.runtime.contracts.Device.

``PlaywrightDevice`` attaches to a running Chrome for Testing over the Chrome
DevTools Protocol (CDP), or launches its own headless Chromium for background/CI
runs, then drives the active page through a desktop pointer + keyboard.  It
satisfies the neutral ``Device`` Protocol (connect/close/screenshot/tap/type_text/
drag/press_home) AND the optional ``ScrollableDevice`` capability (scroll), plus
the browser-only extras ``navigate(url)`` / ``go_back()``.

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

import ipaddress
import json
import os
import signal
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional
from urllib.parse import urlsplit

# Default viewport used to denormalize coordinates and for screenshots when the
# real page viewport cannot be read (CDP pages frequently report viewport_size as
# None for the user's existing tab). Typical laptop content size.
_DEFAULT_VIEWPORT_W = 1280
_DEFAULT_VIEWPORT_H = 800
_NAVIGATION_TIMEOUT_MS = 20_000
TEXT_RETARGET_RADIUS_PX = 220
_CDP_PROXY_ENV_LOCK = threading.Lock()


def _direct_cdp_host(cdp_url: str) -> str:
    """Return a host that should bypass HTTP proxies, or an empty string."""

    host = urlsplit(cdp_url).hostname or ""
    if host.casefold() == "localhost":
        return host
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return ""
    return host if address.is_private or address.is_loopback else ""


@contextmanager
def _cdp_proxy_bypass(cdp_url: str) -> Iterator[None]:
    """Let the Playwright driver reach a local/private CDP endpoint directly."""

    host = _direct_cdp_host(cdp_url)
    if not host:
        yield
        return
    bypass_hosts = {host}
    if host in {"localhost", "127.0.0.1", "::1"}:
        bypass_hosts.update({"localhost", "127.0.0.1", "::1"})
    with _CDP_PROXY_ENV_LOCK:
        previous = {name: os.environ.get(name) for name in ("NO_PROXY", "no_proxy")}
        try:
            for name, value in previous.items():
                entries = {item.strip() for item in (value or "").split(",") if item.strip()}
                os.environ[name] = ",".join(sorted(entries | bypass_hosts))
            yield
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

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
_SETTLE_NAV_CAP_S = 6.0
_FORM_VALUE_FINGERPRINT_JS = (
    "(()=>{const els=[...document.querySelectorAll('input,select,textarea')];"
    "const vals=els.map(e=>(e.type==='checkbox'||e.type==='radio')?(e.checked?'1':'0')"
    ":String(e.value??''));"
    "return els.length+'|'+vals.join('\\u0001');})()"
)

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
    + "const b=document.body;const visible=!!b&&(b.innerText.trim().length>0||"
    "[...b.querySelectorAll('a,button,input,textarea,select,img,canvas,svg,video,iframe')]"
    ".some(e=>e.getClientRects().length>0&&getComputedStyle(e).visibility!=='hidden'));"
    "return [document.readyState,Math.round(performance.now()-window.__q.t),visible];})()"
)

# Same-origin XHR/fetch feedback belongs to the executed GUI action's observable outcome.  Some
# web widgets return a structured rejection without rendering its message into the current
# viewport.  Install a page-local, bounded monitor immediately before dispatch and consume it
# after settle; it observes existing application requests without issuing any request itself.
_ACTION_FEEDBACK_INSTALL_JS = r"""
(() => {
  window.__guiAgentActionFeedback = [];
  const record = (kind, url, status, body) => {
    let text = '';
    try {
      text = typeof body === 'string' ? body : JSON.stringify(body ?? '');
    } catch (e) {
      text = String(body ?? '');
    }
    if (!text.trim()) return;
    window.__guiAgentActionFeedback.push({
      kind: String(kind || ''),
      url: String(url || ''),
      status: Number(status || 0),
      body: text.slice(0, 2000),
    });
    if (window.__guiAgentActionFeedback.length > 10) {
      window.__guiAgentActionFeedback.shift();
    }
  };
  if (!window.__guiAgentXhrFeedbackInstalled) {
    window.__guiAgentXhrFeedbackInstalled = true;
    const originalOpen = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function(method, url) {
      this.__guiAgentUrl = url;
      this.addEventListener('loadend', () => {
        let body = '';
        try {
          body = this.responseType === '' || this.responseType === 'text'
            ? this.responseText
            : this.response;
        } catch (e) {}
        record('xhr', this.responseURL || this.__guiAgentUrl || '', this.status, body);
      }, {once: true});
      return originalOpen.apply(this, arguments);
    };
  }
  if (!window.__guiAgentFetchFeedbackInstalled && typeof window.fetch === 'function') {
    window.__guiAgentFetchFeedbackInstalled = true;
    const originalFetch = window.fetch;
    window.fetch = async function() {
      const response = await originalFetch.apply(this, arguments);
      try {
        const body = await response.clone().text();
        record('fetch', response.url, response.status, body);
      } catch (e) {}
      return response;
    };
  }
  return true;
})()
"""
_ACTION_FEEDBACK_CONSUME_JS = r"""
(() => {
  const rows = Array.isArray(window.__guiAgentActionFeedback)
    ? window.__guiAgentActionFeedback.slice(-10)
    : [];
  window.__guiAgentActionFeedback = [];
  return rows;
})()
"""


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
        headless: bool | None = None,
        user_data_dir: Optional[str] = None,
    ):
        from gui_agent.adapters.browser.factory import _resolve_headless

        # Resolution: explicit arg -> env CHROME_CDP_URL -> localhost:9222.
        self.cdp_url = cdp_url or os.environ.get("CHROME_CDP_URL") or "http://localhost:9222"
        self.start_url = start_url
        self.headless = _resolve_headless(headless)
        self.user_data_dir = user_data_dir or os.environ.get("BROWSER_USER_DATA_DIR")
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
        self._native_action_feedback: list[dict] = []
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
        """Attach to running Chrome over CDP, or launch a headless browser.

        CDP mode reuses the first existing context/page if present (the user's
        tab); headless mode owns a fresh browser/context/page and closes it on
        ``close()``.
        """
        from playwright.sync_api import sync_playwright

        # The Playwright Node driver inherits proxy variables when spawned. CDP is
        # a direct control channel, so ensure that driver bypasses the proxy for
        # its configured endpoint without changing the parent process permanently.
        with _cdp_proxy_bypass(self.cdp_url if not self.headless else ""):
            self._pw = sync_playwright().start()
        if self.headless:
            viewport = {"width": _DEFAULT_VIEWPORT_W, "height": _DEFAULT_VIEWPORT_H}
            if self.user_data_dir:
                profile_dir = Path(self.user_data_dir).expanduser()
                profile_dir.mkdir(parents=True, exist_ok=True)
                self._context = self._pw.chromium.launch_persistent_context(
                    str(profile_dir),
                    headless=True,
                    viewport=viewport,
                )
                self._browser = self._context.browser
            else:
                self._browser = self._pw.chromium.launch(headless=True)
                self._context = self._browser.new_context(
                    viewport=viewport
                )
            pages = [p for p in self._context.pages if not _page_closed(p)]
            self.page = pages[0] if pages else self._context.new_page()
        else:
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
        self._prev_pages = self._all_pages() or [self.page]
        self._tab_switched = False
        self._last_viewport = None
        self._dpr = None
        if self.start_url:
            self.navigate(self.start_url)
        return self

    def close(self):
        """Close owned headless browsers; otherwise detach from Chrome.

        CDP mode only stops the Playwright driver (which closes the CDP
        transport); the attached Chrome and its tabs keep running.
        """
        if self.headless and self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
        if self.headless and self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
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
        return self._timed_cdp_send(self._cdp, method, params)

    def _timed_cdp_send(self, session, method: str, params: dict) -> dict:
        """Send a raw CDP command — NO signal-based timeout.

        A SIGALRM cap used to guard this, but interrupting an in-flight Playwright sync call with a
        signal corrupts the greenlet↔asyncio bridge so the NEXT call hangs forever
        (playwright-python #1150) — that was the real cause of the WebArena 702 slow-save freezes
        (settle's capped CDP send fired SIGALRM during the ~25-60s Catalog reindex → the following
        call hung indefinitely). A raw send has no native timeout, so it blocks naturally; a slow-save
        reindex pins the renderer for the save duration then the send returns cleanly (verified live:
        wait_for_url waited out a 25s reindex and observation resumed normally). Navigation-triggering
        actions are additionally waited out with a NATIVE Playwright wait in wait_settled, which is
        the clean, corruption-free backstop."""
        return session.send(method, params)

    def _cdp_screenshot(self) -> bytes:
        import base64

        result = self._cdp_send("Page.captureScreenshot", {"format": "png"})
        return base64.b64decode(result["data"])

    def is_loading(self) -> bool:
        """Whether the document or a tracked one-shot request is still pending."""
        try:
            self._ensure_net_tracking()
            res = self._cdp_send(
                "Runtime.evaluate",
                {"expression": "document.readyState", "returnByValue": True},
            )
            return (
                (res.get("result", {}) or {}).get("value") == "loading"
                or bool(self._xhr_ids)
            )
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
            self._timed_cdp_send(self._cdp, "Network.enable", {})

            def _on_req(p):
                if p.get("type") in ("XHR", "Fetch"):
                    self._xhr_ids[p.get("requestId")] = time.monotonic()
                    self._xhr_last = time.monotonic()

            def _on_done(p):
                if self._xhr_ids.pop(p.get("requestId"), None) is not None:
                    self._xhr_last = time.monotonic()

            def _on_response(p):
                _on_done(p)

            self._cdp.on("Network.requestWillBeSent", _on_req)
            self._cdp.on("Network.responseReceived", _on_response)
            self._cdp.on("Network.loadingFinished", _on_done)
            self._cdp.on("Network.loadingFailed", _on_done)
            self._net_session = self._cdp
        except Exception:
            # Network tracking is an optimization for settle. If enabling it hangs/fails, mark this
            # session as handled so wait_settled degrades to DOM/readyState instead of retrying the
            # same failing Network.enable on every poll.
            self._net_session = self._cdp
            self._xhr_ids.clear()

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
            rs, q, content_ready = (
                (val[0], val[1], bool(val[2]))
                if isinstance(val, list) and len(val) >= 3
                else ("complete", None, True)
            )
            # XHR/Fetch remains in flight until response headers, completion or failure.  A slow
            # persistence request must not become "quiet" merely because a local timer elapsed.
            now = time.monotonic()
            xhr_inflight = len(self._xhr_ids)
            net_quiet = xhr_inflight == 0 and (now - self._xhr_last) * 1000.0 >= _SETTLE_QUIET_MS
            elapsed = time.perf_counter() - t0
            settled = bool(
                rs != "loading"
                and (q or 0) >= _SETTLE_QUIET_MS
                and net_quiet
                and (action_type != "navigate" or content_ready)
            )
            cap = _SETTLE_NAV_CAP_S if action_type == "navigate" else _SETTLE_CAP_S
            if settled or elapsed >= cap:
                # DOM never mutated after entry → quietMs climbed with elapsed (≈ equal).
                no_effect = q is not None and q >= elapsed * 1000.0 - 150 and xhr_inflight == 0
                tag = "settled" if settled else "达上限·仍在加载"
                print(f"  [Settle] {elapsed:.1f}s (CDP {tag}: readyState={rs}, "
                      f"domQuiet={q}ms, xhr在飞={xhr_inflight}"
                      + (f"，内容={'就绪' if content_ready else '空白'}" if action_type == "navigate" else "")
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

    def platform_time(self):
        """Read the browser environment's local clock and timezone through CDP."""

        from gui_agent.core.runtime.clock import (
            host_time_fallback,
            platform_time_from_parts,
        )

        expression = """(() => {
          const now = new Date();
          const minutes = -now.getTimezoneOffset();
          const sign = minutes >= 0 ? '+' : '-';
          const pad = value => String(Math.abs(value)).padStart(2, '0');
          const offset = sign + pad(Math.trunc(minutes / 60)) + ':' + pad(minutes % 60);
          const local = [
            now.getFullYear(), '-', pad(now.getMonth() + 1), '-', pad(now.getDate()), 'T',
            pad(now.getHours()), ':', pad(now.getMinutes()), ':', pad(now.getSeconds()), offset
          ].join('');
          return {
            local_datetime: local,
            timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || '',
            utc_offset: offset
          };
        })()"""
        try:
            result = self._cdp_send(
                "Runtime.evaluate",
                {"expression": expression, "returnByValue": True},
            )
            value = (result.get("result") or {}).get("value") or {}
            if not all(value.get(key) for key in ("local_datetime", "utc_offset")):
                raise ValueError("CDP clock response is incomplete")
            return platform_time_from_parts(
                "browser",
                local_datetime=str(value["local_datetime"]),
                timezone_name=str(value.get("timezone") or value["utc_offset"]),
                utc_offset=str(value["utc_offset"]),
                source="browser_cdp",
            )
        except Exception as exc:  # noqa: BLE001 - clock has explicit fallback provenance
            return host_time_fallback(
                "browser",
                reason=f"browser clock unavailable: {type(exc).__name__}",
            )

    def form_state_fingerprint(self) -> str | None:
        """Hash form values and checked state, excluding transient focus.

        ``Observation.dom_state`` is a progress signal. Moving focus only proves that a pointer
        or keyboard action changed the active element; it does not prove that any target value
        changed. Including focus allowed repeated no-op writes to masquerade as progress.
        """
        try:
            res = self._cdp_send(
                "Runtime.evaluate",
                {"expression": _FORM_VALUE_FINGERPRINT_JS, "returnByValue": True},
            )
            val = (res.get("result", {}) or {}).get("value")
        except Exception:
            return None
        if not isinstance(val, str) or not val:
            return None
        import hashlib

        return hashlib.md5(val.encode("utf-8")).hexdigest()[:16]

    def read_applied_filter_state(self) -> tuple[dict[str, str] | None, dict[str, object] | None]:
        """The grid's currently-applied filters as `{label: value}` plus extraction metadata.

        This is the deterministic "which filters are in effect" signal behind
        Observation.applied_filters. Browser-specific extraction details are normalized to the
        platform-neutral metadata contract before leaving this adapter. Raw CDP is used because
        page.evaluate is broken over connect_over_cdp."""
        from gui_agent.adapters.browser.filter_state import (
            applied_filters_js,
            normalize_applied_filter_state,
        )

        try:
            res = self._cdp_send(
                "Runtime.evaluate",
                {"expression": applied_filters_js(), "returnByValue": True},
            )
            val = (res.get("result", {}) or {}).get("value")
        except Exception:
            return None, {
                "source": "read_failed",
                "indicator_channel": "unknown",
                "fallback_channel": "unknown",
            }
        return normalize_applied_filter_state(val)

    def read_applied_filters(self) -> dict[str, str] | None:
        """Compatibility wrapper returning only the platform-neutral applied-filter mapping."""
        filters, _meta = self.read_applied_filter_state()
        return filters

    def read_tables(self) -> list[dict]:
        """Return structured DOM table/grid snapshots from the current page.

        This is a read-only perception sensor. It does not click, scroll, export,
        or download; it only serializes rows already present in the live DOM.
        """
        from gui_agent.adapters.browser.table_reader import (
            normalize_table_snapshots,
            normalize_viewport,
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
            self._last_traversal_viewport = normalize_viewport(raw)
            return normalize_table_snapshots(raw)
        except Exception:
            self._last_traversal_viewport = None
            return []

    def read_viewport(self) -> dict | None:
        """Return the page-level traversal/scroll-boundary signal from the last read_tables() call.

        NOT to be confused with ``viewport_size`` (CSS-px width/height for click denorm,
        cached in ``self._last_viewport``) — different concept, deliberately different name."""
        return getattr(self, "_last_traversal_viewport", None)

    def read_form_controls(self) -> list[dict]:
        """Return visible form controls with DOM type/value/option metadata."""
        from gui_agent.adapters.browser.form_reader import (
            form_controls_js,
            normalize_form_control_state,
            normalize_form_control_snapshot,
        )

        try:
            self._follow_active_tab()
            res = self._cdp_send(
                "Runtime.evaluate",
                {"expression": form_controls_js(), "returnByValue": True},
            )
            val = (res.get("result", {}) or {}).get("value")
            raw = json.loads(val) if isinstance(val, str) else val
            controls, metadata = normalize_form_control_snapshot(raw)
            state, state_metadata = normalize_form_control_state(raw)
            self._last_form_controls_meta = metadata
            self._last_form_control_state = state
            self._last_form_control_state_meta = state_metadata
            return controls
        except Exception:
            self._last_form_controls_meta = None
            self._last_form_control_state = None
            self._last_form_control_state_meta = None
            return []

    def read_form_controls_meta(self) -> dict | None:
        """Return coverage metadata from the last form-control snapshot."""
        return getattr(self, "_last_form_controls_meta", None)

    def read_form_control_state(self) -> list[dict]:
        """Return the complete control-state index from the last DOM snapshot."""
        return list(getattr(self, "_last_form_control_state", None) or [])

    def read_form_control_state_meta(self) -> dict | None:
        """Return coverage metadata for the complete control-state index."""
        return getattr(self, "_last_form_control_state_meta", None)

    def read_semantic_tree(self) -> list[dict]:
        """Return a pruned flat AX-tree snapshot of the current page.

        Uses CDP ``Accessibility.getFullAXTree``; each node is
        ``{role, key, value, ref, depth}``.  Scroll-position independent —
        the full page structure is returned regardless of what is currently
        in the viewport.  Returns [] on any failure.
        """
        from gui_agent.adapters.browser.semantic_page import build_semantic_tree

        try:
            self._follow_active_tab()
            return build_semantic_tree(self._cdp_send)
        except Exception:
            return []

    def click_by_ref(self, backend_node_id: int) -> str:
        """Click a DOM element by its ``backendDOMNodeId`` (from the semantic tree).

        Resolves the node via CDP ``DOM.resolveNode`` then invokes ``.click()``
        directly on the JS object — no coordinate arithmetic, no viewport scroll
        required.  Returns ``"ok"`` on success or ``"failed: <reason>"`` on any
        error (stale ref, navigation mid-flight, etc.).
        """
        try:
            self._follow_active_tab()
            resolve = self._cdp_send("DOM.resolveNode", {"backendNodeId": backend_node_id})
            obj_id = (resolve.get("object") or {}).get("objectId")
            if not obj_id:
                return "failed: could not resolve backendNodeId"
            self._cdp_send(
                "Runtime.callFunctionOn",
                {
                    "objectId": obj_id,
                    "functionDeclaration": (
                        "function(){this.scrollIntoView({block:'nearest',behavior:'instant'});"
                        "this.click();}"
                    ),
                    "returnByValue": True,
                },
            )
            return "ok"
        except Exception as exc:
            return f"failed: {exc}"

    def scroll_to_ref(self, backend_node_id: int) -> str:
        """Scroll a DOM element into the viewport center by its ``backendDOMNodeId``.

        Uses ``scrollIntoView({block:'center',behavior:'instant'})`` — one jump,
        no incremental scrolling.  Returns ``"ok"`` or ``"failed: <reason>"``.
        """
        try:
            self._follow_active_tab()
            resolve = self._cdp_send("DOM.resolveNode", {"backendNodeId": backend_node_id})
            obj_id = (resolve.get("object") or {}).get("objectId")
            if not obj_id:
                return "failed: could not resolve backendNodeId"
            self._cdp_send(
                "Runtime.callFunctionOn",
                {
                    "objectId": obj_id,
                    "functionDeclaration": (
                        "function(){this.scrollIntoView({block:'center',behavior:'instant'});}"
                    ),
                    "returnByValue": True,
                },
            )
            return "ok"
        except Exception as exc:
            return f"failed: {exc}"

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
    def begin_action_feedback(self) -> None:
        """Start a bounded same-origin response monitor for the next GUI action."""
        self._native_action_feedback = []
        try:
            self._follow_active_tab()
            self._cdp_send(
                "Runtime.evaluate",
                {"expression": _ACTION_FEEDBACK_INSTALL_JS, "returnByValue": True},
            )
        except Exception:
            return

    def consume_action_feedback(self) -> list[dict]:
        """Return response payloads caused since :meth:`begin_action_feedback`."""
        native = getattr(self, "_native_action_feedback", [])
        self._native_action_feedback = []
        if native:
            return native
        try:
            self._follow_active_tab()
            result = self._cdp_send(
                "Runtime.evaluate",
                {"expression": _ACTION_FEEDBACK_CONSUME_JS, "returnByValue": True},
            )
            value = (result.get("result", {}) or {}).get("value")
            return (
                [item for item in value if isinstance(item, dict)]
                if isinstance(value, list)
                else []
            )
        except Exception:
            return []

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

    def select_option(self, x: Optional[float], y: Optional[float], option_text: str) -> str:
        """Select an option: mouse-click for rendered dropdowns, JS only for native ``<select>``.

        Two paths by what the screenshot can see (mouse interaction is the default — most
        general and natural; JS is only the fallback when CDP can't render the control):
        - native ``<select>``: option popups are outside the page rendering tree (CDP can't
          capture them, the mouse can't target them), so set value + dispatch input/change
          via JS — the ONLY case where we bypass the mouse.
        - custom dropdown (ARIA/listbox/Magento selectmenu): options ARE rendered in the
          page, so find the option's coordinates and ``page.mouse.click`` them — the
          physical click fires the full mousedown→mouseup→click chain and works for any
          handler binding (click/checked), no need to guess which element holds the handler.
        """
        self._follow_active_tab()
        try:
            page = self._require_page()
            try:
                page.bring_to_front()
            except Exception:
                pass
            result = page.evaluate(
                r"""({x, y, target}) => {
                    const norm = (v) => String(v ?? '')
                        .replace(/\s+/g, ' ')
                        .trim()
                        .toLowerCase();
                    const wanted = norm(target);
                    const labelOf = (el) => String(
                        el?.innerText || el?.textContent || el?.value || ''
                    ).replace(/\s+/g, ' ').trim();
                    const byPoint = (
                        Number.isFinite(x) && Number.isFinite(y)
                    ) ? document.elementFromPoint(x, y) : null;

                    function selectAtPointOrFocus() {
                        let el = byPoint;
                        if (el && el.tagName === 'OPTION') el = el.parentElement;
                        if (el?.closest) {
                            const direct = el.closest('select');
                            if (direct) return direct;
                        }
                        const active = document.activeElement;
                        if (active?.tagName === 'SELECT') return active;
                        return null;
                    }

                    const select = selectAtPointOrFocus();
                    if (select) {
                        const options = Array.from(select.options || []);
                        const option = options.find((opt) => {
                            return norm(opt.textContent) === wanted || norm(opt.value) === wanted;
                        }) || options.find((opt) => {
                            return norm(opt.textContent).includes(wanted) || norm(opt.value).includes(wanted);
                        });
                        if (!option) {
                            return {
                                ok: false,
                                reason: `option not found: ${target}`,
                                options: options.map((opt) => labelOf(opt)).filter(Boolean).slice(0, 20),
                            };
                        }
                        // <select multiple> (e.g. Magento Cart Price Rule "Customer Groups"):
                        // setting select.value REPLACES the whole selection (deselects the rest),
                        // so selecting groups one-per-call would leave only the last. Add to the
                        // selection instead. Single-select keeps the value= path. Verified live via
                        // CDP on /sales_rule/promo_quote/new/ (customer_group_ids).
                        if (select.multiple) {
                            option.selected = true;
                        } else {
                            select.value = option.value;
                            option.selected = true;
                        }
                        select.dispatchEvent(new Event('input', {bubbles: true}));
                        select.dispatchEvent(new Event('change', {bubbles: true}));
                        return {
                            ok: true,
                            mode: 'native',
                            label: labelOf(option),
                            value: option.value,
                        };
                    }

                    const candidates = Array.from(document.querySelectorAll(
                        '[role=option], [role=menuitem], [role=listbox] li, .admin__action-dropdown-menu li, .selectmenu li, li, option'
                    )).filter((el) => {
                        if (el.closest('select')) return false;
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    });
                    const option = candidates.find((el) => norm(labelOf(el)) === wanted)
                        || candidates.find((el) => norm(labelOf(el)).includes(wanted));
                    if (option) {
                        // option 是页面渲染的自定义下拉项(CDP 截得到) → 返回其坐标,由调用方
                        // page.mouse.click 物理点击(鼠标优先:触发完整 mousedown→mouseup→click 事件链,
                        // 不挑 click/checked 绑定,是最通用自然的交互;只有 native <select> 的 option 不
                        // 在渲染树才用 JS)。优先叶子 button/a(挂 handler 的元素)的坐标,避免点到外层容器。
                        const leaf = option.querySelector('button, a, [data-bind*="click"]')
                            || (option.matches('button, a, [data-bind*="click"]') ? option : null);
                        const hit = leaf || option;
                        const r = hit.getBoundingClientRect();
                        return {ok: true, mode: 'mouse', x: r.x + r.width/2, y: r.y + r.height/2, label: labelOf(option)};
                    }

                    return {
                        ok: false,
                        reason: 'no select at point/focus and no visible matching option',
                    };
                }""",
                {"x": x, "y": y, "target": option_text},
            )
        except Exception as exc:  # noqa: BLE001
            return f"failed: {exc}"
        if not isinstance(result, dict):
            return f"failed: unexpected result {result!r}"
        if not result.get("ok"):
            detail = result.get("reason") or "unknown"
            options = result.get("options")
            if options:
                detail = f"{detail}; options={options}"
            return f"failed: {detail}"
        label = result.get("label") or option_text
        mode = result.get("mode") or "native"
        if mode == "mouse":
            # 自定义下拉:option 渲染在页面 → 物理鼠标点 option 坐标(鼠标优先,JS 只留 native <select>)
            mx, my = result.get("x"), result.get("y")
            if mx is None or my is None:
                return f"failed: mouse target missing coordinates"
            try:
                mx, my = float(mx), float(my)
            except (TypeError, ValueError):
                return f"failed: mouse target not finite ({mx!r},{my!r})"
            page.mouse.click(mx, my)
            return f"OK select_option {label!r} (mouse)"
        # native <select>:option 不在渲染树,JS 已在上方 set value + dispatch change
        value = result.get("value")
        suffix = f" value={value!r}" if value is not None else ""
        return f"OK select_option {label!r} (select){suffix}"

    def dom_snap(
        self, x: float, y: float, target_text: str = ""
    ) -> "tuple[float, float, Optional[str]]":
        """Snap a viewport-CSS click point to the center of the small clickable element under it
        — browser's coordinate correction, the DOM analogue of iphone's YOLO snap.

        TEXT RETARGET (the OCR-snap analogue): when ``target_text`` is given (the label quoted
        in the action description, e.g. 「操作」) and the element under the point carries a
        DIFFERENT label, search nearby for a clickable whose exact trimmed text equals
        the target and snap there instead. This rescues the adjacent-menu-item failure mode:
        two 28px-high items (操作/删除), the vision model's y is one row off, and the click
        lands on the DESTRUCTIVE neighbour (run 20260612_114219: 3× clicked 删除 aiming 操作).
        The radius also covers tall browser side menus where the model can be several rows
        off (WebArena task 64: aiming Orders, hitting Credit Memos). It still keeps same-label
        elements elsewhere on the page (e.g. a table header) out.

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
            "const R=%d;"
            "const CLICK='a,button,input,select,textarea,label,[role=button],"
            "[role=option],[role=menuitem],[role=tab],[role=checkbox],[role=radio],[onclick],.cursor-pointer,li';"
            # Page-wide retarget accepts only interactive accessible identities.
            "const RETARGET='a,button,summary,label,input[type=button],input[type=submit],"
            "input[type=reset],[role=button],[role=link],[role=option],[role=menuitem],"
            "[role=menuitemcheckbox],[role=menuitemradio],[role=tab],[role=checkbox],"
            "[role=radio],[onclick],.cursor-pointer';"
            "const SEARCH=RETARGET+',input,select,textarea';"
            "const norm=s=>((s||'')+'').replace(/\\s+/g,' ').trim().toLowerCase();"
            "const accessibleName=e=>norm((e.getAttribute&&"
            "(e.getAttribute('aria-label')||e.getAttribute('title')))"
            "||e.innerText||e.textContent||'');"
            "const textMatch=e=>!!(e&&e.matches&&e.matches(RETARGET)"
            "&&accessibleName(e)===norm(target));"
            # Form controls match identity, never their current value.
            "const T=norm(target);"
            "const roleT=/(?<![a-z])to(?![a-z])|\\bmax\\b|上限|截止/.test(T)?'to':"
            "(/(?<![a-z])from(?![a-z])|\\bmin\\b|下限|起始/.test(T)?'from':'');"
            "const nameToks=T.replace(/(?<![a-z])(from|to|min|max)(?![a-z])|输入框|输入|框|区间|范围|的/g,' ')"
            ".split(/\\s+/).filter(Boolean);"
            "const idOf=e=>{let p=[e.getAttribute&&e.getAttribute('name'),"
            "e.getAttribute&&e.getAttribute('aria-label'),e.getAttribute&&e.getAttribute('placeholder'),e.id];"
            "if(e.id){try{const l=document.querySelector('label[for=\"'+(window.CSS&&CSS.escape?CSS.escape(e.id):e.id)+'\"]');"
            "if(l)p.push(l.textContent);}catch(_){}}"
            "const box=e.closest&&e.closest('.admin__field,.admin__form-field,.field,[data-role=filter],td,th,div');"
            "if(box){const l=box.querySelector('label,.admin__field-label,.label');if(l)p.push(l.textContent);}"
            "return norm(p.filter(Boolean).join(' '));};"
            "const roleOf=e=>{const n=norm((e.getAttribute&&e.getAttribute('name'))||'')+' '+"
            "norm((e.getAttribute&&e.getAttribute('placeholder'))||'')+' '+"
            "norm((e.getAttribute&&e.getAttribute('aria-label'))||'');"
            "if(/\\[to\\]|(?<![a-z])to(?![a-z])|_to|-to|\\bmax\\b/.test(n))return 'to';"
            "if(/\\[from\\]|(?<![a-z])from(?![a-z])|_from|-from|\\bmin\\b/.test(n))return 'from';return '';};"
            "const isField=e=>['input','textarea','select'].includes((e.tagName||'').toLowerCase());"
            "const matchCtl=e=>{if(!isField(e)||!nameToks.length)return false;const id=idOf(e);"
            "const ok=nameToks.every(t=>id.includes(t)||(t==='quantity'&&id.includes('qty'))||"
            "(t==='qty'&&id.includes('quantity')));"
            "if(!ok)return false;return roleT?roleOf(e)===roleT:true;};"
            "let el=document.elementFromPoint(x,y);if(!el)return '';"
            "const n=el.closest&&el.closest(CLICK);"
            "if(target && (!n || (!textMatch(n) && !matchCtl(n)))){"
            "  let best=null,bd=1e9;"
            "  for(const c of document.querySelectorAll(SEARCH)){"
            "    if(!textMatch(c) && !matchCtl(c))continue;"
            "    const r=c.getBoundingClientRect();"
            "    if(r.width<=0||r.height<=0||r.bottom<=0||r.right<=0"
            "      ||r.top>=innerHeight||r.left>=innerWidth)continue;"
            "    const cx=r.x+r.width/2,cy=r.y+r.height/2,dd=Math.hypot(cx-x,cy-y);"
            "    if(cx<0||cy<0||cx>=innerWidth||cy>=innerHeight)continue;"
            "    if(dd<bd){bd=dd;best={cx:Math.round(cx),cy:Math.round(cy),"
            "      w:Math.round(r.width),h:Math.round(r.height)};}}"
            "  if(best&&bd<=R)return JSON.stringify({...best,tag:'text'});"
            "}"
            "if(!n)return '';const tag=n.tagName.toLowerCase();"
            "const role=(n.getAttribute&&n.getAttribute('role'))||'';"
            "const itype=tag==='input'?((n.getAttribute('type')||'').toLowerCase()):'';"
            "if(['canvas','svg','video','html','body','main','form'].includes(tag))return '';"
            "if(['slider','scrollbar'].includes(role)||itype==='range')return '';"
            "const r=n.getBoundingClientRect(),vw=innerWidth,vh=innerHeight;"
            "if(r.width<=0||r.height<=0||r.width>vw*0.9||r.height>vh*0.6)return '';"
            "const cx=r.x+r.width/2,cy=r.y+r.height/2;"
            "if(r.bottom<=0||r.right<=0||r.top>=vh||r.left>=vw"
            "  ||cx<0||cy<0||cx>=vw||cy>=vh)return '';"
            "return JSON.stringify({cx:Math.round(cx),cy:Math.round(cy),"
            "tag,w:Math.round(r.width),h:Math.round(r.height)});})()"
            % (int(round(x)), int(round(y)), target_js, TEXT_RETARGET_RADIUS_PX)
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
            cx, cy = float(d["cx"]), float(d["cy"])
            width, height = self.viewport_size
            if not (0 <= cx < width and 0 <= cy < height):
                return x, y, None
            return cx, cy, f"{d['tag']} {d['w']}x{d['h']}"
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

    def safe_scroll_anchor(
        self,
        x: float,
        y: float,
        target_area: str = "main_content",
    ) -> tuple[float, float, str] | None:
        """Find a non-control wheel anchor near the requested browser region.

        Wheel events are delivered to the element under the pointer. If a default point lands on
        a select, textarea, listbox, or another control, that control can consume the wheel while
        the page remains fixed. This read-only DOM probe chooses a visible non-control point.
        """
        self._follow_active_tab()
        page = self._require_page()
        try:
            result = page.evaluate(
                """({preferredX, preferredY, area}) => {
                  const ranges = {
                    main_content: [0.18, 0.94, 0.12, 0.88],
                    left_panel: [0.02, 0.45, 0.12, 0.88],
                    right_panel: [0.55, 0.98, 0.12, 0.88],
                    top_content: [0.10, 0.90, 0.05, 0.48],
                    bottom_content: [0.10, 0.90, 0.52, 0.95],
                  };
                  const [x0, x1, y0, y1] = ranges[area] || ranges.main_content;
                  const unsafe = [
                    'a', 'button', 'input', 'textarea', 'select', 'option',
                    '[contenteditable="true"]', '[draggable="true"]',
                    '[role="button"]', '[role="checkbox"]', '[role="combobox"]',
                    '[role="listbox"]', '[role="menu"]', '[role="menuitem"]',
                    '[role="option"]', '[role="radio"]', '[role="scrollbar"]',
                    '[role="slider"]', '[role="spinbutton"]', '[role="switch"]',
                    '[role="tab"]', '[role="textbox"]'
                  ].join(',');
                  const preferredInside = (
                    preferredX >= innerWidth * x0 && preferredX <= innerWidth * x1 &&
                    preferredY >= innerHeight * y0 && preferredY <= innerHeight * y1
                  );
                  const candidates = preferredInside ? [[preferredX, preferredY]] : [];
                  for (const yf of [0.5, 0.35, 0.65, 0.2, 0.8]) {
                    for (const xf of [0.82, 0.68, 0.54, 0.40, 0.26]) {
                      const px = innerWidth * (x0 + (x1 - x0) * xf);
                      const py = innerHeight * (y0 + (y1 - y0) * yf);
                      candidates.push([px, py]);
                    }
                  }
                  let best = null;
                  for (const [px, py] of candidates) {
                    const el = document.elementFromPoint(px, py);
                    if (!el || el.closest(unsafe)) continue;
                    const style = getComputedStyle(el);
                    if (style.visibility === 'hidden' || style.display === 'none') continue;
                    const score = Math.hypot(px - preferredX, py - preferredY);
                    if (!best || score < best.score) {
                      best = {x: Math.round(px), y: Math.round(py),
                              tag: (el.tagName || '').toLowerCase(), score};
                    }
                  }
                  return best;
                }""",
                {
                    "preferredX": float(x),
                    "preferredY": float(y),
                    "area": target_area or "main_content",
                },
            )
        except Exception:
            return None
        if not isinstance(result, dict):
            return None
        try:
            return float(result["x"]), float(result["y"]), str(result.get("tag") or "")
        except (KeyError, TypeError, ValueError):
            return None

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
        d = (direction or "").strip().lower()
        viewport_w, viewport_h = self.viewport_size
        horizontal = d in (
            "left", "right", "leftward", "rightward", "向左", "向右",
        )
        axis_extent = viewport_w if horizontal else viewport_h
        # Keep context from the prior frame visible. A single wheel action larger
        # than the viewport can skip table headers, section labels, and other
        # identity evidence the next frame needs for grounding.
        dist = min(
            max(1, int(amount)) * _SCROLL_PX_PER_AMOUNT,
            max(1, round(axis_extent * 0.9)),
        )
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
    def _navigation_failure(self, url: str, error: object) -> str:
        message = (
            str(error or "unknown navigation error")
            .split("\nCall log:", 1)[0]
            .strip()
        )
        message = message[:500] or "unknown navigation error"
        self._native_action_feedback = [{
            "kind": "navigation",
            "url": url,
            "status": 0,
            "body": json.dumps({"error": True, "message": message}),
        }]
        return f"failed: navigate {url}: {message}"

    def navigate(self, url: str) -> str:
        try:
            if self.headless:
                self._require_page().goto(
                    url,
                    wait_until="commit",
                    timeout=_NAVIGATION_TIMEOUT_MS,
                )
            else:
                result = self._cdp_send("Page.navigate", {"url": url})
                if error := str(result.get("errorText") or "").strip():
                    return self._navigation_failure(url, error)
        except Exception as exc:  # noqa: BLE001
            return self._navigation_failure(url, exc)
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
            contexts = self._browser.contexts if self._browser is not None else [self._context]
            for ctx in contexts:
                if ctx is None:
                    continue
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
