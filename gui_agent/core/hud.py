"""AgentHUD — neutral floating heads-up display showing live agent action status.

Platform-neutral presentation widget: a borderless, always-on-top, optionally
translucent panel rendered at a given screen position and driven by a status
string. Each platform's factory decides WHERE to place it (iphone: below the
mirror window; browser: over the Chrome window) and HOW opaque (``alpha``) — this
module only renders. It draws OUTSIDE any captured surface (its own OS window), so
it never appears in the agent's perception screenshots.

Runs as a small tkinter subprocess that polls a status file. The file's first line
carries geometry ("<x> <y> <w> <h>") and the rest is the status text, so the panel
can be REPOSITIONED at runtime (e.g. after the browser connects and the exact
window rect is known via CDP) without a second IPC channel.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

_CLOSE_SENTINEL = "__CLOSE__"


def _hud_main(x: int, y: int, w: int, h: int, alpha: float, status_file: str) -> None:
    import tkinter as tk

    root = tk.Tk()
    root.title("")
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    try:
        root.attributes("-alpha", alpha)  # whole-window translucency (macOS supports this)
    except Exception:
        pass
    root.configure(bg="#0d0d1a")

    cur_geo = [""]

    def apply_geo(gx: int, gy: int, gw: int, gh: int) -> None:
        geo = f"{gw}x{gh}+{gx}+{gy}"
        if geo != cur_geo[0]:
            cur_geo[0] = geo
            root.geometry(geo)
            root.update_idletasks()
            root.geometry(geo)

    apply_geo(x, y, w, h)

    label = tk.Label(
        root,
        text="等待 Agent 启动…",
        bg="#0d0d1a",
        fg="#00d4ff",
        font=("Helvetica Neue", 11),
        wraplength=max(60, w - 20),
        justify="left",
        anchor="nw",
        padx=8,
        pady=6,
    )
    label.pack(fill="both", expand=True)

    path = Path(status_file)
    last = [""]

    def _poll() -> None:
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            root.destroy()
            return
        if content.strip() == _CLOSE_SENTINEL:
            root.destroy()
            return
        # "<x> <y> <w> <h>\n<status text...>" — first line geometry, rest text.
        text = content
        head, sep, body = content.partition("\n")
        parts = head.split()
        if sep and len(parts) == 4:
            try:
                gx, gy, gw, gh = (int(p) for p in parts)
                apply_geo(gx, gy, gw, gh)
                label.config(wraplength=max(60, gw - 20))
                text = body
            except ValueError:
                pass
        text = text.strip()
        if text != last[0]:
            last[0] = text
            label.config(text=text)
        root.after(150, _poll)

    root.after(150, _poll)
    root.mainloop()


class AgentHUD:
    """Floating status panel at an explicit screen position. Platform factories
    compute the position/opacity; ``reposition`` moves it at runtime."""

    def __init__(
        self,
        *,
        origin: tuple[int, int] = (100, 100),
        width: int = 318,
        height: int = 72,
        alpha: float = 1.0,
    ) -> None:
        self._x, self._y = int(origin[0]), int(origin[1])
        self._w, self._h = int(width), int(height)
        self._header = ""  # persistent top line (the task goal), above live status
        self._text = "等待 Agent 启动…"

        fd, tmp = tempfile.mkstemp(suffix=".txt")
        os.close(fd)
        self._status_file = Path(tmp)
        self._write()

        self._proc = subprocess.Popen(
            [
                sys.executable, __file__,
                str(self._x), str(self._y), str(self._w), str(self._h),
                str(float(alpha)), str(self._status_file),
            ],
        )

    def _write(self) -> None:
        # Body = optional goal header + live status, both shown in the panel (the
        # tkinter side renders everything after the geometry line as wrapped text).
        body = f"🎯 {self._header}\n{self._text}" if self._header else self._text
        try:
            self._status_file.write_text(
                f"{self._x} {self._y} {self._w} {self._h}\n{body}",
                encoding="utf-8",
            )
        except OSError:
            pass

    def update(self, text: str) -> None:
        if self._proc.poll() is not None:
            return
        self._text = text
        self._write()

    def set_goal(self, goal: str) -> None:
        """Set a persistent header (the task goal) shown above the live status.
        Single-lined and length-capped so the panel stays bounded."""
        g = (goal or "").strip().replace("\n", " ")
        if len(g) > 60:
            g = g[:59] + "…"
        self._header = g
        self._write()

    def reposition(self, x: int, y: int, width: int | None = None, height: int | None = None) -> None:
        """Move/resize the panel (e.g. once the exact window rect is known)."""
        self._x, self._y = int(x), int(y)
        if width is not None:
            self._w = int(width)
        if height is not None:
            self._h = int(height)
        self._write()

    def close(self) -> None:
        try:
            self._status_file.write_text(_CLOSE_SENTINEL, encoding="utf-8")
        except OSError:
            pass
        try:
            self._proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._proc.terminate()
        try:
            self._status_file.unlink(missing_ok=True)
        except OSError:
            pass

    def __enter__(self) -> "AgentHUD":
        return self

    def __exit__(self, *_) -> None:
        self.close()


def dock_rect(
    x: int, y: int, w: int, h: int,
    *, bar_h: int = 100, max_w: int = 420, bottom_frac: float = 0.08,
) -> tuple[int, int, int, int]:
    """HUD placement inside a window rect: a horizontally-CENTERED panel sitting LOW
    (a margin above the bottom edge, not flush). ``bar_h`` is tall enough to fit the
    goal header (1-2 wrapped lines) plus the live status line. Returns (hx, hy, hw,
    hh). Shared by the browser factory's pre-connect guess and the runner's
    post-connect reposition so both agree."""
    hw = min(max(w - 32, 160), max_w)
    hx = x + (w - hw) // 2
    hy = y + h - bar_h - max(40, int(h * bottom_frac))
    return hx, hy, hw, bar_h


if __name__ == "__main__":
    _hud_main(
        int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]),
        float(sys.argv[5]), sys.argv[6],
    )
