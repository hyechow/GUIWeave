"""AgentHUD — floating heads-up display showing live agent action status."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path


_CLOSE_SENTINEL = "__CLOSE__"


def _hud_main(x: int, y: int, status_file: str) -> None:
    import tkinter as tk

    root = tk.Tk()
    root.title("")
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg="#0d0d1a")

    geo = f"318x72+{x}+{y}"
    root.geometry(geo)
    root.update()
    root.geometry(geo)

    label = tk.Label(
        root,
        text="等待 Agent 启动…",
        bg="#0d0d1a",
        fg="#00d4ff",
        font=("Helvetica Neue", 11),
        wraplength=298,
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
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            root.destroy()
            return
        if text == _CLOSE_SENTINEL:
            root.destroy()
            return
        if text != last[0]:
            last[0] = text
            label.config(text=text)
        root.after(150, _poll)

    root.after(150, _poll)
    root.mainloop()


class AgentHUD:
    """Floating HUD panel showing live agent status below the iPhone mirror window."""

    def __init__(self) -> None:
        from policy_expr.adapters.iphone.executor import _find_iphone_window

        pos = _find_iphone_window()
        if pos:
            ox, oy = int(pos[0]), int(pos[1]) + 701 + 2
        else:
            ox, oy = 100, 100

        fd, tmp = tempfile.mkstemp(suffix=".txt")
        os.close(fd)
        self._status_file = Path(tmp)
        self._status_file.write_text("等待 Agent 启动…", encoding="utf-8")

        self._proc = subprocess.Popen(
            [sys.executable, __file__, str(ox), str(oy), str(self._status_file)],
        )

    def update(self, text: str) -> None:
        if self._proc.poll() is not None:
            return
        try:
            self._status_file.write_text(text, encoding="utf-8")
        except OSError:
            pass

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


if __name__ == "__main__":
    x, y, status_file = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
    _hud_main(x, y, status_file)
