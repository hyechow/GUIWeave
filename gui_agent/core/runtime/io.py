"""Run directories, stdio capture, and interactive stop handling."""

from __future__ import annotations

import os
import select
import sys
import termios
import threading
import traceback
import tty
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from typing import IO, Iterator

ROOT = Path(__file__).resolve().parents[3]
ESC_SEQUENCE_WINDOW_S = 0.04


def get_log_root() -> Path:
    """Resolve the log root after callers have loaded their environment files."""

    return Path(
        os.environ.get("GUIWEAVE_LOG_ROOT", ROOT / "logs" / "gui_agent")
    ).expanduser().resolve()


def create_run_dir(
    mode: str,
    platform: str = "",
    *,
    log_root: Path | None = None,
) -> Path:
    # logs/gui_agent/<mode>/<platform>/<ts>/ keeps platform runs separate.
    started_at = datetime.now().strftime("%Y%m%d_%H%M%S")
    root = (log_root or get_log_root()).expanduser().resolve()
    base = root / mode / platform if platform else root / mode
    path = base / started_at
    suffix = 2
    while path.exists():
        path = base / f"{started_at}_{suffix}"
        suffix += 1
    path.mkdir(parents=True, exist_ok=True)
    return path


class TeeStream:
    """Write text to both the original stream and a log file."""

    def __init__(self, original: IO[str], log_file: IO[str]) -> None:
        self._original = original
        self._log_file = log_file
        self.encoding = getattr(original, "encoding", "utf-8")
        self.errors = getattr(original, "errors", "replace")

    def write(self, text: str) -> int:
        written = self._original.write(text)
        self._log_file.write(text)
        return written

    def flush(self) -> None:
        self._original.flush()
        self._log_file.flush()

    def isatty(self) -> bool:
        return self._original.isatty()

    def fileno(self) -> int:
        return self._original.fileno()

    def __getattr__(self, name: str) -> object:
        return getattr(self._original, name)


@contextmanager
def tee_stdio(log_dir: Path) -> Iterator[None]:
    """Mirror stdout/stderr to per-run text logs while preserving terminal output."""

    stdout_path = log_dir / "stdout.log"
    stderr_path = log_dir / "stderr.log"
    with (
        stdout_path.open("a", encoding="utf-8", buffering=1) as stdout_file,
        stderr_path.open("a", encoding="utf-8", buffering=1) as stderr_file,
        redirect_stdout(TeeStream(sys.stdout, stdout_file)),
        redirect_stderr(TeeStream(sys.stderr, stderr_file)),
    ):
        print(f"Stdout  : {stdout_path}")
        print(f"Stderr  : {stderr_path}")
        try:
            yield
        except Exception:
            traceback.print_exc()
            raise SystemExit(1) from None


@contextmanager
def capture_stdio(log_dir: Path) -> Iterator[None]:
    """Capture runtime output without touching stdout used by stdio MCP."""

    stdout_path = log_dir / "stdout.log"
    stderr_path = log_dir / "stderr.log"
    with (
        stdout_path.open("a", encoding="utf-8", buffering=1) as stdout_file,
        stderr_path.open("a", encoding="utf-8", buffering=1) as stderr_file,
        redirect_stdout(stdout_file),
        redirect_stderr(stderr_file),
    ):
        yield


class EscStopSignal:
    """Capture Esc in auto-run mode and let the main loop stop after the turn."""

    def __init__(self, enabled: bool = True) -> None:
        self._want_enabled = enabled
        self._enabled = False
        self._fd: int | None = None
        self._old_attrs: list | None = None
        self._running = False
        self._requested = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    def requested(self) -> bool:
        return self._requested.is_set()

    def __enter__(self) -> "EscStopSignal":
        if not self._want_enabled or not sys.stdin.isatty():
            return self
        try:
            fd = sys.stdin.fileno()
            old_attrs = termios.tcgetattr(fd)
            tty.setcbreak(fd)
        except (OSError, termios.error):
            return self
        self._fd = fd
        self._old_attrs = old_attrs
        self._running = True
        self._enabled = True
        self._thread = threading.Thread(
            target=self._watch,
            name="esc-stop",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._running = False
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=0.2)
        if self._fd is not None and self._old_attrs is not None:
            try:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_attrs)
            except termios.error:
                pass

    def _watch(self) -> None:
        assert self._fd is not None
        while self._running and not self._requested.is_set():
            try:
                readable, _, _ = select.select([self._fd], [], [], 0.1)
            except (OSError, ValueError):
                return
            if not readable:
                continue
            try:
                ch = os.read(self._fd, 1)
            except OSError:
                return
            if not ch:
                return
            if ch == b"\x1b" and _is_standalone_escape(self._fd):
                self._requested.set()


def _is_standalone_escape(fd: int, *, sequence_window_s: float = ESC_SEQUENCE_WINDOW_S) -> bool:
    """Return true for a lone Esc key, false for escape sequences like arrows/Alt keys."""
    try:
        readable, _, _ = select.select([fd], [], [], max(sequence_window_s, 0.0))
    except (OSError, ValueError):
        return True
    if not readable:
        return True
    try:
        extra = os.read(fd, 32)
    except OSError:
        return True
    return not extra
