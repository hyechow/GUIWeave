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

    # Two stacked zones. The TASK (fixed goal) is secondary — small, muted. The agent
    # DECISION (live turn status) is the FOCUS — larger, bold, bright — since it is
    # what changes every turn. Each under a small dim caption, separated by a hairline.
    BG, GOAL_FG, STATUS_FG, TAG_FG, SEP_FG = "#0d0d1a", "#98a3b5", "#7aa2ff", "#6b7894", "#233048"
    outer = tk.Frame(root, bg=BG)
    outer.pack(fill="both", expand=True, padx=14, pady=11)

    tk.Label(outer, text="任务", bg=BG, fg=TAG_FG, font=("Helvetica Neue", 9), pady=0).pack(anchor="w")
    # Single-line goal: the task is fixed, secondary context — one line is enough
    # (set_goal elides if longer). NO wraplength → it never wraps, stays one line,
    # and leaves the vertical room for the decision zone below.
    goal_label = tk.Label(
        outer, text="—", bg=BG, fg=GOAL_FG, font=("Helvetica Neue", 11),
        justify="left", anchor="w", pady=2,
    )
    goal_label.pack(anchor="w", fill="x", pady=(2, 0))
    tk.Frame(outer, bg=SEP_FG, height=1).pack(fill="x", pady=4)
    tk.Label(outer, text="决策", bg=BG, fg=TAG_FG, font=("Helvetica Neue", 9), pady=0).pack(anchor="w")
    # Text (not Label) for the decision: a Label's multi-line spacing is font-fixed and
    # the two wrapped lines sit too tight — Text's ``spacing2`` opens the gap between
    # them. ``spacing1/3`` + pady keep the first/last line off the edges.
    status_label = tk.Text(
        outer, bg=BG, fg=STATUS_FG, font=("Helvetica Neue", 12, "bold"), wrap="char",
        bd=0, highlightthickness=0, padx=0, pady=2,
        spacing1=2, spacing2=9, spacing3=2, cursor="arrow", takefocus=0,
    )
    status_label.insert("1.0", "等待 Agent 启动…")
    status_label.configure(state="disabled")
    status_label.pack(anchor="w", fill="both", expand=True, pady=(1, 0))

    path = Path(status_file)
    last = [("", "")]

    def _poll() -> None:
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            root.destroy()
            return
        if content.strip() == _CLOSE_SENTINEL:
            root.destroy()
            return
        # "<x> <y> <w> <h>\n<goal>\n<status...>" — line 1 geometry, line 2 goal, rest status.
        head, nl, body = content.partition("\n")
        parts = head.split()
        if nl and len(parts) == 4:
            try:
                gx, gy, gw, gh = (int(p) for p in parts)
                apply_geo(gx, gy, gw, gh)  # goal Label / status Text wrap by their own width
            except ValueError:
                body = content
        else:
            body = content
        goal, _, status = body.partition("\n")
        goal, status = goal.strip(), status.strip()
        if (goal, status) != last[0]:
            last[0] = (goal, status)
            goal_label.config(text=(goal or "—"))
            status_label.configure(state="normal")
            status_label.delete("1.0", "end")
            status_label.insert("1.0", status or "…")
            status_label.configure(state="disabled")
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
        # "<geo>\n<goal>\n<status>": the renderer splits line 2 (goal) from the rest
        # (status) and styles each zone; prefixes/emoji are added renderer-side.
        goal = (self._header or "").strip()
        status = (self._text or "").strip()
        try:
            self._status_file.write_text(
                f"{self._x} {self._y} {self._w} {self._h}\n{goal}\n{status}",
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
        if len(g) > 34:
            g = g[:33] + "…"
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
    *, bar_h: int = 155, max_w: int = 600, bottom_frac: float = 0.08,
) -> tuple[int, int, int, int]:
    """HUD placement inside a window rect: a horizontally-CENTERED panel sitting LOW
    (a margin above the bottom edge, not flush). ``bar_h`` fits the goal (1-2 lines)
    plus a 1-2 line decision; the width is the window width capped at ``max_w``.
    Returns (hx, hy, hw, hh). Shared by the browser factory's pre-connect guess and
    the runner's post-connect reposition so both agree."""
    hw = min(max(w - 32, 160), max_w)
    hx = x + (w - hw) // 2
    # 100px minimum keeps the HUD clear of the macOS Dock (~80px) at the bottom.
    hy = y + h - bar_h - max(100, int(h * bottom_frac))
    return hx, hy, hw, bar_h


if __name__ == "__main__":
    _hud_main(
        int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]),
        float(sys.argv[5]), sys.argv[6],
    )
