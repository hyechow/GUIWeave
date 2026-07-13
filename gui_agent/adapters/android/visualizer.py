"""Android action visualizer: the iphone/browser ``agent_cursor`` blue arrow,
glided over the scrcpy mirror window at each action's location.

Implements the neutral :class:`gui_agent.core.runtime.contracts.ActionVisualizer`. It reuses
the SAME ``sck/agent_cursor`` OS overlay that iphone and browser drive — only the
coordinate mapping differs (here: device-normalized 0-1000 -> the scrcpy window's
content area on the Mac screen).

WHY THE RUNNER SEAM (not the device layer like iphone). iphone drives its cursor
inside the device (MirrorDaemonClient) for two iphone-only reasons: it needs the
POST-snap tap coords (YOLO/OCR snapping runs in the executor) and coverage of every
runner path. Android has NEITHER — it is vision-only (no snapping, so the policy
coords ARE the tap coords) with a single action path — so the cursor belongs here,
as PURE visualization fully DECOUPLED from perception/control. It is a Mac OS overlay
and the perception is adb ``screencap`` of the phone's own framebuffer, so the cursor
can never enter the agent's screenshot.

WHY IT NEEDS scrcpy. The agent itself has no Mac window (it perceives via adb); the
only on-screen mirror to draw on is an (optional) scrcpy window. If scrcpy is not
running the window lookup returns None and ``show_action`` is a clean no-op — the
agent is unaffected. Everything here is best-effort and MUST NOT raise into the loop.

macOS-host only (agent_cursor is a Swift NSWindow; window lookup is Quartz).
"""

from __future__ import annotations

from gui_agent.core.schemas import BaseAction

# Action types with a screen location worth a cursor. press_enter / home /
# clear_text are non-spatial and skipped (no misleading center flash).
_SPATIAL = {"tap", "click", "type", "scroll", "drag"}

# Content direction -> agent_cursor arrow mode (same convention as the browser cursor).
_SCROLL_MODE = {
    "up": "scroll_up",
    "down": "scroll_down",
    "left": "scroll_left",
    "right": "scroll_right",
}

# Standard macOS titled-window title-bar height (points). scrcpy's SDL window has a
# normal title bar by default; CGWindowBounds includes it, so the phone content area
# starts this far below the window's top. Best-effort constant (borderless windows
# would be off by this much — a cosmetic shift only).
_TITLE_BAR = 28


def scrcpy_window_rect() -> "tuple[int, int, int, int] | None":
    """(x, y, w, h) screen rect (top-left origin, points) of the largest on-screen
    scrcpy window via CGWindowList, or None if not found / Quartz unavailable.
    macOS-host only; imported lazily so importing this module stays Quartz-free."""
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
        if "scrcpy" not in str(w.get("kCGWindowOwnerName", "")).lower():
            continue
        b = w.get("kCGWindowBounds", {})
        ww, wh = int(b.get("Width", 0)), int(b.get("Height", 0))
        if ww > 80 and wh > 120 and ww * wh > best_area:
            best_area = ww * wh
            best = (int(b.get("X", 0)), int(b.get("Y", 0)), ww, wh)
    return best


class AndroidActionVisualizer:
    """Glide the shared agent_cursor over the scrcpy window at each action point."""

    name = "android_cursor"

    def __init__(self, session):
        # ``session`` is an AndroidSession; the device (with win_w/win_h) is ``.client``.
        self._session = session
        self._cursor = None       # AgentCursor once started
        self._disabled = False    # set if the binary/daemon is unavailable

    def _ensure_cursor(self) -> None:
        if self._cursor is not None or self._disabled:
            return
        try:
            import atexit
            import sys
            from pathlib import Path

            # agent_cursor lives in sck/ (shared device-I/O infra); import it the same
            # way the iphone daemon and the browser cursor do.
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
            # Keep the cursor resident at the last point — the OS overlay never enters
            # the agent's adb screenshot, so the idle auto-hide is unnecessary.
            self._cursor.persist(True)
            atexit.register(self.clear)  # daemon also auto-exits on stdin EOF
        except Exception:
            self._disabled = True

    def _screen_point(self, norm_x: float, norm_y: float):
        """Map normalized 0-1000 device coords -> macOS screen point (top-left origin),
        re-querying the scrcpy window each call (handles window move/resize). Returns
        None when no scrcpy window is on screen."""
        rect = scrcpy_window_rect()
        if rect is None:
            return None
        wx, wy, ww, wh = rect
        # Content area = window minus the title bar.
        cx0, cy0 = wx, wy + _TITLE_BAR
        cw, ch = ww, wh - _TITLE_BAR
        if cw <= 0 or ch <= 0:
            return None
        # The phone (device pixels) is aspect-fit (letterboxed) inside the content area.
        client = getattr(self._session, "client", None)
        dw = float(getattr(client, "win_w", 0) or 1080)
        dh = float(getattr(client, "win_h", 0) or 2400)
        dev_aspect = dw / dh
        if cw / ch > dev_aspect:        # content wider than phone -> bars left/right
            disp_h = ch
            disp_w = disp_h * dev_aspect
            off_x, off_y = (cw - disp_w) / 2, 0.0
        else:                           # content taller -> bars top/bottom
            disp_w = cw
            disp_h = disp_w / dev_aspect
            off_x, off_y = 0.0, (ch - disp_h) / 2
        px = norm_x / 1000.0 * disp_w
        py = norm_y / 1000.0 * disp_h
        return cx0 + off_x + px, cy0 + off_y + py

    def show_action(self, action: BaseAction) -> None:
        action_type = getattr(action, "action_type", None)
        if action_type not in _SPATIAL:
            return
        # A type into the already-focused field (no coords) has no point to flash.
        if action_type == "type" and (action.x is None or action.y is None):
            return
        self._ensure_cursor()
        if self._cursor is None:
            return
        # scroll/drag may omit coords -> anchor at screen center.
        nx = action.x if action.x is not None else 500
        ny = action.y if action.y is not None else 500
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
