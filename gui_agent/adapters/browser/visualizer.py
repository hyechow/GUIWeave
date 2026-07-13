"""Browser action visualizer: drives the agent_cursor OS overlay over the real Chrome window.

Implements the neutral :class:`gui_agent.core.runtime.contracts.ActionVisualizer` for the
browser platform.
"""

from __future__ import annotations

from gui_agent.core.schemas import Action

# Action types that have a screen location worth visualizing. press_enter / home /
# clear_text is non-spatial and is skipped (no misleading center flash).
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
