"""Framed stdio clients for GUIWeave's local iPhone Mirroring helpers."""

from __future__ import annotations

import os
import struct
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def _helper_path(environment_name: str, fallback_name: str) -> Path:
    configured = os.environ.get(environment_name, "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return ROOT / "bin" / fallback_name


MIRROR_DAEMON = _helper_path("GUIWEAVE_MIRROR_DAEMON", "mirror_daemon")
SCK_SERVER = _helper_path("GUIWEAVE_SCK_SERVER", "sck_server")
IPHONE_VIEWPORT = (318, 701)


class _FramedProcess:
    def __init__(self, executable: Path) -> None:
        self.executable = executable
        self._process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        if not self.executable.is_file():
            raise FileNotFoundError(f"required helper not found: {self.executable}")
        self._process = subprocess.Popen(
            [str(self.executable)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
        )
        assert self._process.stdout is not None
        ready = self._process.stdout.readline().strip()
        if ready != b"ready":
            self.close()
            raise RuntimeError(
                f"{self.executable.name} failed to start: {ready!r}"
            )

    def close(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)

    def _read_exact(self, size: int) -> bytes:
        process = self._process
        if process is None or process.stdout is None:
            raise RuntimeError(f"{self.executable.name} is not running")
        value = bytearray()
        while len(value) < size:
            chunk = process.stdout.read(size - len(value))
            if not chunk:
                raise RuntimeError(f"{self.executable.name} closed unexpectedly")
            value.extend(chunk)
        return bytes(value)

    def command(self, command: str) -> bytes:
        process = self._process
        if process is None or process.stdin is None:
            raise RuntimeError(f"{self.executable.name} is not running")
        process.stdin.write((command + "\n").encode("utf-8"))
        process.stdin.flush()
        length = struct.unpack(">I", self._read_exact(4))[0]
        return self._read_exact(length) if length else b""


class SCKScreenshotClient:
    """The sole iPhone screenshot source, using only ``sck_server``."""

    def __init__(self, executable: Path = SCK_SERVER) -> None:
        self._transport = _FramedProcess(executable)

    def start(self) -> None:
        self._transport.start()

    def screenshot(self, retries: int = 10) -> bytes:
        for _ in range(retries):
            frame = self._transport.command("screenshot")
            if frame:
                return frame
            time.sleep(0.2)
        raise RuntimeError("sck_server returned no iPhone Mirroring frame")

    def close(self) -> None:
        self._transport.close()


class MirrorDaemonClient:
    """The sole iPhone input client, using only ``mirror_daemon``."""

    viewport_size = IPHONE_VIEWPORT

    def __init__(self, executable: Path = MIRROR_DAEMON) -> None:
        self._transport = _FramedProcess(executable)

    def connect(self) -> None:
        self._transport.start()

    def close(self) -> None:
        self._transport.close()

    def _text(self, command: str) -> str:
        value = self._transport.command(command).decode("utf-8", errors="replace")
        if value.strip().lower().startswith(("err", "error")):
            return f"failed: {value}"
        return value

    def tap(self, x: float, y: float) -> str:
        return self._text(f"tap {round(x)} {round(y)}")

    def type_text(self, text: str) -> str:
        value = text.replace("\r", " ").replace("\n", " ")
        return self._text(f"type {value}")

    def clear_text(self) -> str:
        select = self._text("key a command")
        delete = self._text("key delete")
        return f"{select}; {delete}"

    def press_enter(self) -> str:
        return self._text("key return")

    def scroll(
        self,
        direction: str,
        amount: int = 5,
        x: float = 159,
        y: float = 350,
    ) -> str:
        return self._text(
            f"scroll {direction} {int(amount)} {round(x)} {round(y)}"
        )

    def drag(
        self,
        from_x: float,
        from_y: float,
        to_x: float,
        to_y: float,
        duration_ms: int = 1000,
    ) -> str:
        steps = max(3, min(60, round(duration_ms / 16)))
        step_ms = max(1, round(duration_ms / steps))
        return self._text(
            "drag "
            f"{round(from_x)} {round(from_y)} {round(to_x)} {round(to_y)} "
            f"{steps} {step_ms}"
        )

    def press_home(self) -> str:
        return self._text("menu home")

    def app_switch(self) -> str:
        return self._text("menu appswitch")

    def status(self) -> str:
        return self._text("status")


__all__ = [
    "IPHONE_VIEWPORT",
    "MIRROR_DAEMON",
    "SCK_SERVER",
    "MirrorDaemonClient",
    "SCKScreenshotClient",
]
