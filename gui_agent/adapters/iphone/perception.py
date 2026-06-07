"""Environment sensing for policy experiments."""

import io
import os
import struct
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

from gui_agent.adapters.iphone.client import SyncMCPClient, MirrorDaemonClient
from gui_agent.core.schemas import Observation

ROOT = Path(__file__).resolve().parents[3]
SCREENSHOT = ROOT / "logs" / "gui_agent" / "scratch" / "screenshot.png"
_SCK_SERVER = ROOT / "bin" / "sck_server"
_MASK_PATH = Path(__file__).parent / "assets" / "mcp_frame_mask.png"

WIN_W = 318
WIN_H = 701

# Load mask once: 255 = device frame (black out), 0 = screen content (keep)
_FRAME_MASK: np.ndarray | None = (
    np.array(Image.open(_MASK_PATH).convert("L"))
    if _MASK_PATH.exists() else None
)


def _apply_mcp_frame(png_bytes: bytes) -> bytes:
    """Apply the MCP device-frame mask to a raw SCK screenshot.

    Pixels where the mask is non-zero are blacked out, matching mirror-mcp's
    device chrome (Dynamic Island, rounded corners, border) exactly.
    Falls back to returning the image unchanged if no mask file exists.
    """
    if _FRAME_MASK is None:
        return png_bytes
    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    arr = np.array(img)
    arr[_FRAME_MASK > 0] = 0
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    return buf.getvalue()


class SCKSession:
    """Manages the SCK stream server subprocess for low-flash screenshot capture.

    The stream starts once (triggering the iOS recording indicator once).
    Subsequent screenshot() calls grab frames from the live stream silently.
    """

    def __init__(self):
        self._proc: subprocess.Popen | None = None

    def start(self):
        if not _SCK_SERVER.exists():
            raise FileNotFoundError(
                f"sck_server binary not found at {_SCK_SERVER}. "
                "Run: swiftc sck/sck_stream_server.swift ... -o bin/sck_server"
            )
        self._proc = subprocess.Popen(
            [str(_SCK_SERVER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
        )
        # Wait for "ready\n" on stdout — confirms stream is up
        assert self._proc.stdout
        line = self._proc.stdout.readline()
        if line.strip() != b"ready":
            raise RuntimeError(f"SCK server failed to start: {line!r}")

    def screenshot(self, retries: int = 10) -> bytes:
        assert self._proc and self._proc.stdin and self._proc.stdout
        for _ in range(retries):
            self._proc.stdin.write(b"screenshot\n")
            self._proc.stdin.flush()
            raw = self._proc.stdout.read(4)
            if len(raw) < 4:
                raise RuntimeError("SCK server closed unexpectedly")
            (length,) = struct.unpack(">I", raw)
            if length > 0:
                return _apply_mcp_frame(self._proc.stdout.read(length))
            time.sleep(0.2)
        raise RuntimeError("SCK server: no frame available after retries")

    def close(self):
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except Exception:
                self._proc.kill()
            self._proc = None
            self._proc = None


class LivePhoneSession:
    """拥有执行用的输入连接 + 截图源。后端由 AGENT_MODE 选择:

    - "daemon"(默认):MirrorDaemonClient 一体提供截图 + 零抢占输入 + agent 光标 overlay
      (截图前自动隐藏光标,不进感知图)。tap/home/screenshot 走 daemon;scroll/drag/
      type 仍走 executor 的现有路径(Quartz/osascript),够先测 tap。MIRROR_CURSOR=0 关光标。
    """

    def __init__(self, backend: str | None = None):
        self.client: SyncMCPClient | MirrorDaemonClient | None = None
        self._sck: SCKSession | None = None
        self._screenshot_via_client = False   # daemon 后端:截图走 client 而非 SCK
        self._backend = backend  # explicit override; None → fall back to AGENT_MODE env

    def __enter__(self) -> "LivePhoneSession":
        _MODE_MAP = {"silent": "daemon", "standard": "mirroir"}
        raw = (self._backend or os.environ.get("AGENT_MODE", "silent")).lower()
        backend = _MODE_MAP.get(raw, raw)  # silent→daemon, standard→mirroir, else pass through
        if backend == "daemon":
            if MirrorDaemonClient is None:
                raise RuntimeError("MirrorDaemonClient 不可用(检查 bin/mirror_daemon 与依赖)")
            print("连接手机中 (mirror_daemon, 零抢占)...")
            cursor = os.environ.get("MIRROR_CURSOR", "1") != "0"
            self.client = MirrorDaemonClient(cursor=cursor)
            self.client.connect()
            self._screenshot_via_client = True
            print("mirror_daemon 连接成功")
        else:
            print("启动 SCK 截图流...")
            sck = SCKSession()
            sck.start()
            self._sck = sck
            print("SCK 截图流就绪")
            print("连接手机中 (mirroir-mcp)...")
            self.client = SyncMCPClient()
            self.client.connect()
            print("MCP 连接成功")
        return self

    def __exit__(self, *_):
        if self._sck:
            self._sck.close()
            self._sck = None
        if self.client:
            self.client.close()
        self.client = None

    def screenshot(self) -> bytes:
        if self._screenshot_via_client:
            assert self.client is not None
            return self.client.screenshot()
        if not self._sck:
            raise RuntimeError("SCK 尚未连接")
        return self._sck.screenshot()


def mirroring_window_bounds() -> tuple[float, float, float, float] | None:
    """Return (x, y, w, h) of the iPhone mirroring window, or None if not found."""
    try:
        import Quartz
        windows = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID
        )
        for w in windows:
            if "iphone" not in w.get("kCGWindowOwnerName", "").lower():
                continue
            b = w.get("kCGWindowBounds", {})
            if b.get("Width", 0) > 100 and b.get("Height", 0) > 400:
                return b["X"], b["Y"], b["Width"], b["Height"]
    except Exception:
        pass
    return None


def dismiss_iphone_sheet() -> bool:
    """Dismiss a Mac-side AXSheet in the iPhone Mirroring window via AXPress (zero-preempt).

    Handles dialogs like "无法从 Mac 使用 iPhone 相机/麦克风".
    Returns True if a sheet was found and pressed, False otherwise.
    """
    try:
        import Cocoa
        import ApplicationServices as AX

        def _ax(el, attr):
            err, val = AX.AXUIElementCopyAttributeValue(el, attr, None)
            return val if err == 0 else None

        for app in Cocoa.NSWorkspace.sharedWorkspace().runningApplications():
            if "iphone" not in (app.localizedName() or "").lower():
                continue
            root = AX.AXUIElementCreateApplication(app.processIdentifier())
            for win in (_ax(root, "AXWindows") or []):
                for child in (_ax(win, "AXChildren") or []):
                    if _ax(child, "AXRole") != "AXSheet":
                        continue
                    for btn in (_ax(child, "AXChildren") or []):
                        if _ax(btn, "AXRole") == "AXButton":
                            AX.AXUIElementPerformAction(btn, "AXPress")
                            time.sleep(0.4)
                            return True
        return False
    except Exception:
        return False


class LivePerception:
    """Capture the current phone screen through an active live session."""

    def __init__(self, phone: LivePhoneSession, screenshot_path: Path = SCREENSHOT):
        self.phone = phone
        self.screenshot_path = screenshot_path

    def observe(self) -> Observation:
        print("截图中...")
        png_bytes = self.phone.screenshot()
        self.screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        self.screenshot_path.write_bytes(png_bytes)
        print(f"截图大小: {len(png_bytes) // 1024} KB，已保存到 {self.screenshot_path}")
        return Observation(png_bytes=png_bytes, source="live")
