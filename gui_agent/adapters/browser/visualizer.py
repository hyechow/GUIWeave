"""Browser action visualizer: a live, auto-fading DOM overlay drawn on the page.

Implements the neutral :class:`gui_agent.core.runtime.contracts.ActionVisualizer` for the
browser platform. Each ``show_action`` injects (idempotently) a small overlay node
into the page and animates it at the action's location, so a human watching the
real Chrome sees WHERE / WHAT the agent is doing:
  - tap / type  -> a ripple ring (+ a dot, + a label for typed text)
  - scroll      -> a ring + a "scroll <dir>" label at the scroll anchor
  - drag        -> a line traced from the start point to the end point

RENDERED VIA RAW CDP. We inject through the device's ``eval_js`` (raw CDP
``Runtime.evaluate``), NOT Playwright's ``page.evaluate`` — the latter hangs
forever when the high-level execution-context binding fails over connect_over_cdp
(Playwright<->Chrome version skew). ``eval_js`` is best-effort and never raises, so
the cosmetic overlay can never stall the agent loop. The JS body is shipped as a
function and called as an IIFE with a JSON-serialized argument object.

WHY AUTO-FADE (not a persistent cursor). The overlay lives in the page DOM, so a
persistent cursor would show up in the agent's OWN screenshot perception every turn
and could confuse the action policy. Instead each mark animates from opaque to
transparent over ~0.7-0.8s; by the time the next turn's perception fires (after the
action + the runner's ~1s+ settle wait) it is already invisible — the "hide before
perception" requirement, met WITHOUT coupling to the perception code.

WHY RE-INJECT EVERY CALL. Page navigations wipe the previously injected node;
``show_action`` re-creates the style + layer if missing, so the overlay survives
navigation with no separate bookkeeping.

Coordinates are denormalized in the page (``x/1000 * window.innerWidth``), the same
basis the executor uses, so the mark lands where the click does.
"""

from __future__ import annotations

import json

from gui_agent.core.schemas import Action

# Action types that have a screen location worth visualizing. press_enter / home /
# stop / clear_text are non-spatial and are skipped (no misleading center flash).
_SPATIAL = {"tap", "click", "type", "scroll", "drag", "select_option"}


def _visual_point(action: Action) -> tuple[float | None, float | None]:
    """Return the point to visualize, preferring executor-recorded snap output."""
    snap = getattr(action, "snap", None)
    if isinstance(snap, dict):
        snapped = snap.get("snapped")
        if isinstance(snapped, (list, tuple)) and len(snapped) >= 2:
            try:
                return float(snapped[0]), float(snapped[1])
            except (TypeError, ValueError):
                pass
    return action.x, action.y


# A JS function (called as an IIFE with one object arg) that injects the keyframes +
# overlay layer once, then draws the mark for this action.
_OVERLAY_FN = r"""
(arg) => {
  const ID = '__gui_agent_action_overlay__';
  const STYLE_ID = ID + '__style';
  if (!document.getElementById(STYLE_ID)) {
    const st = document.createElement('style');
    st.id = STYLE_ID;
    st.textContent = `
      @keyframes __gaa_ripple {
        0%   { transform: translate(-50%,-50%) scale(0.35); opacity: 0.9; }
        70%  { opacity: 0.5; }
        100% { transform: translate(-50%,-50%) scale(1.7); opacity: 0; }
      }
      @keyframes __gaa_fade { from { opacity: 1; } to { opacity: 0; } }
      #${ID} { position: fixed; left: 0; top: 0; width: 0; height: 0;
               margin: 0; padding: 0; border: 0; pointer-events: none;
               z-index: 2147483647; }
      #${ID} .gaa_ring { position: fixed; width: 54px; height: 54px;
               box-sizing: border-box; border: 3px solid #2f81f7;
               border-radius: 50%; box-shadow: 0 0 12px rgba(47,129,247,.85);
               transform: translate(-50%,-50%);
               animation: __gaa_ripple 700ms ease-out forwards; }
      #${ID} .gaa_dot { position: fixed; width: 12px; height: 12px;
               background: #2f81f7; border-radius: 50%;
               transform: translate(-50%,-50%);
               box-shadow: 0 0 10px rgba(47,129,247,.95);
               animation: __gaa_fade 700ms ease-out forwards; }
      #${ID} .gaa_label { position: fixed; transform: translate(-50%,-210%);
               font: 600 12px -apple-system, system-ui, sans-serif; color: #fff;
               background: #2f81f7; padding: 2px 8px; border-radius: 11px;
               white-space: nowrap; box-shadow: 0 1px 4px rgba(0,0,0,.3);
               animation: __gaa_fade 800ms ease-out forwards; }
      #${ID} .gaa_line { position: fixed; height: 3px; background: #2f81f7;
               transform-origin: 0 50%; box-shadow: 0 0 8px rgba(47,129,247,.85);
               animation: __gaa_fade 800ms ease-out forwards; }
    `;
    (document.head || document.documentElement).appendChild(st);
  }
  let layer = document.getElementById(ID);
  if (!layer) {
    layer = document.createElement('div');
    layer.id = ID;
    (document.body || document.documentElement).appendChild(layer);
  }
  while (layer.firstChild) layer.removeChild(layer.firstChild);
  const W = window.innerWidth || 1280, H = window.innerHeight || 800;
  const X = (arg.x == null ? 500 : arg.x) / 1000 * W;
  const Y = (arg.y == null ? 500 : arg.y) / 1000 * H;
  const mk = (cls, x, y) => {
    const e = document.createElement('div');
    e.className = cls;
    if (x != null) e.style.left = x + 'px';
    if (y != null) e.style.top = y + 'px';
    layer.appendChild(e);
    return e;
  };
  if (arg.type === 'drag' && arg.toX != null && arg.toY != null) {
    const X2 = arg.toX / 1000 * W, Y2 = arg.toY / 1000 * H;
    const line = mk('gaa_line', X, Y);
    line.style.width = Math.hypot(X2 - X, Y2 - Y) + 'px';
    line.style.transform = 'rotate(' + (Math.atan2(Y2 - Y, X2 - X) * 180 / Math.PI) + 'deg)';
    mk('gaa_dot', X, Y);
    mk('gaa_dot', X2, Y2);
  } else if (arg.type === 'scroll') {
    mk('gaa_ring', X, Y);
    if (arg.label) mk('gaa_label', X, Y).textContent = arg.label;
  } else {
    mk('gaa_ring', X, Y);
    mk('gaa_dot', X, Y);
    if (arg.label) mk('gaa_label', X, Y).textContent = arg.label;
  }
}
"""

_CLEAR_FN = r"""
() => {
  for (const id of ['__gui_agent_action_overlay__', '__gui_agent_action_overlay____style']) {
    const el = document.getElementById(id);
    if (el && el.parentNode) el.parentNode.removeChild(el);
  }
}
"""


def _label_for(action: Action) -> str | None:
    """Short text badge for a mark: typed text for ``type``, direction for
    ``scroll``; none for tap/drag (the ring/line already reads clearly)."""
    if action.action_type == "type":
        text = (action.text or "").strip()
        if not text:
            return None
        return "⌨ " + (text if len(text) <= 18 else text[:17] + "…")
    if action.action_type == "scroll":
        return "scroll " + (action.direction or "")
    if action.action_type == "select_option":
        text = (action.text or "").strip()
        return "select " + (text if len(text) <= 18 else text[:17] + "…")
    return None


class BrowserActionVisualizer:
    """Live page overlay for browser actions (the browser ``ActionVisualizer``)."""

    name = "browser_overlay"

    def __init__(self, session):
        # ``session`` is a BrowserSession; the device (with eval_js) is ``.client``.
        self._session = session

    def _client(self):
        return getattr(self._session, "client", None)

    def show_action(self, action: Action) -> None:
        action_type = getattr(action, "action_type", None)
        if action_type not in _SPATIAL:
            return
        x, y = _visual_point(action)
        # A type into the already-focused field (no coords) has no point to flash.
        if action_type == "type" and (x is None or y is None):
            return
        client = self._client()
        if client is None or not hasattr(client, "eval_js"):
            return
        arg = {
            "type": "tap" if action_type == "click" else action_type,
            "x": x,
            "y": y,
            "toX": action.to_x,
            "toY": action.to_y,
            "label": _label_for(action),
        }
        client.eval_js(f"({_OVERLAY_FN})({json.dumps(arg)})")

    def clear(self) -> None:
        client = self._client()
        if client is None or not hasattr(client, "eval_js"):
            return
        client.eval_js(f"({_CLEAR_FN})()")


# Direction -> agent_cursor arrow mode (content direction is inverted from the
# finger/cursor arrow: scrolling DOWN to see lower content points the arrow down).
_SCROLL_MODE = {
    "up": "scroll_up",
    "down": "scroll_down",
    "left": "scroll_left",
    "right": "scroll_right",
}


class BrowserCursorVisualizer:
    """Reuse the iPhone ``agent_cursor`` OS overlay (the blue gradient arrow) for the
    browser. Instead of injecting into the page DOM, it drives the standalone
    ``sck/agent_cursor`` daemon to glide a virtual cursor over the real Chrome window
    at the action's location.

    WHY THIS IS THE CLEAN ONE (vs the DOM overlay above):
      - Draws OUTSIDE the page, so it NEVER appears in the agent's own
        ``Page.captureScreenshot`` perception (the OS overlay is not part of the web
        contents) — no perception contamination, no hide-before-perception dance.
      - Independent process: immune to the connect_over_cdp execution-context binding
        that hangs ``page.evaluate`` on a Playwright<->Chrome version skew.
      - One renderer shared with iPhone (true cross-platform visualization); only the
        coordinate mapping (page viewport -> macOS screen) differs per platform.

    COORDINATE MAP: screen_point = window_origin + content_offset + page_px, where the
    window screen rect comes from CDP ``Browser.getWindowForTarget`` and the content
    offset (toolbar/tabs height) = window.height - viewport.height. All in CSS px =
    macOS logical points (no devicePixelRatio scaling needed).

    macOS-host only (agent_cursor is a Swift NSWindow). Best-effort throughout: if the
    binary is missing/uncompilable or any CDP/daemon call fails, visualization is
    silently disabled and the agent loop is unaffected.
    """

    name = "browser_cursor"

    def __init__(self, session):
        self._session = session
        self._cursor = None       # AgentCursor once started
        self._disabled = False    # set if the binary/daemon is unavailable

    def _client(self):
        return getattr(self._session, "client", None)

    def _ensure_cursor(self) -> None:
        if self._cursor is not None or self._disabled:
            return
        try:
            import atexit
            import sys
            from pathlib import Path

            # agent_cursor lives in sck/ (the overlay is device-I/O infra, not policy
            # core); import it the same way the iphone daemon does.
            sck_dir = Path(__file__).resolve().parents[3] / "sck"
            if str(sck_dir) not in sys.path:
                sys.path.insert(0, str(sck_dir))
            from agent_cursor import AgentCursor, ensure_cursor_bin

            cb = ensure_cursor_bin()
            if not cb:
                self._disabled = True
                return
            self._cursor = AgentCursor(cb)
            self._cursor.start()
            # Keep the cursor resident at the last action point — the OS overlay
            # never enters the page screenshot (verified), so the idle auto-hide is
            # unnecessary here (iphone, which doesn't call this, keeps its default).
            self._cursor.persist(True)
            atexit.register(self.clear)  # daemon also auto-exits on stdin EOF
        except Exception:
            self._disabled = True

    def _screen_point(self, norm_x: float, norm_y: float):
        """Map normalized 0-1000 page coords -> macOS screen point (top-left origin)."""
        client = self._client()
        if client is None or not hasattr(client, "window_bounds"):
            return None
        wb = client.window_bounds()
        if wb is None:
            return None
        left, top, win_w, win_h = wb
        vw, vh = client.viewport_size
        content_x = left + max(0, win_w - vw) / 2          # side border (≈0 on mac)
        content_y = top + max(0, win_h - vh)               # toolbar + tabs height
        px = norm_x / 1000.0 * vw
        py = norm_y / 1000.0 * vh
        return content_x + px, content_y + py

    def show_action(self, action: Action) -> None:
        action_type = getattr(action, "action_type", None)
        if action_type not in _SPATIAL:
            return
        x, y = _visual_point(action)
        if action_type == "type" and (x is None or y is None):
            return
        self._ensure_cursor()
        if self._cursor is None:
            return
        # scroll/drag may omit coords -> anchor at viewport center.
        nx = x if x is not None else 500
        ny = y if y is not None else 500
        pt = self._screen_point(nx, ny)
        if pt is None:
            return
        try:
            if action_type == "scroll":
                self._cursor.set_mode(_SCROLL_MODE.get((action.direction or "").lower(), "normal"))
            else:
                self._cursor.set_mode("normal")
            self._cursor.move(pt[0], pt[1])
            self._cursor.show()
        except Exception:
            pass

    def clear(self) -> None:
        if self._cursor is not None:
            try:
                self._cursor.close()
            except Exception:
                pass
            self._cursor = None
