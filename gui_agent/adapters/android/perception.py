"""Android perception: session lifecycle + observe(), mirroring the iphone shape.

``AndroidSession`` is the neutral perception/lifecycle surface core holds as
``phone`` (it satisfies ``PerceptionSession``): a context manager that connects an
:class:`AndroidDevice` on ``__enter__``, exposes ``.client`` + ``screenshot()`` and
drops the handle on ``__exit__`` (without killing the adb server).

``AndroidPerception`` wraps the session's screenshot in an ``Observation`` with
``source='android'`` (satisfies ``Perception``). VISION-ONLY: pixels only, no
element_tree / page_key — exactly like the iphone ``LivePerception`` and the
browser ``BrowserPerception``.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Optional

from gui_agent.adapters.android.constants import SCREENSHOT_MAX_WIDTH
from gui_agent.core.schemas import Observation

ROOT = Path(__file__).resolve().parents[3]
SCREENSHOT = ROOT / "logs" / "gui_agent" / "scratch" / "android_screenshot.png"


def _downscale_width(png_bytes: bytes, target_w: int) -> bytes:
    """Downscale PNG bytes to ``target_w`` width (preserving aspect). No-op if the
    target is <= 0 or the image is already that narrow. Best-effort: returns the
    original bytes on any failure (never blocks perception)."""
    if target_w <= 0:
        return png_bytes
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(png_bytes))
        if img.width <= target_w:
            return png_bytes
        target_h = max(1, round(img.height * target_w / img.width))
        resized = img.resize((target_w, target_h), Image.LANCZOS)
        buf = io.BytesIO()
        resized.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return png_bytes


class AndroidSession:
    """Owns the active adb device + screenshot source for the agent loop.

    Mirrors ``LivePhoneSession`` / ``BrowserSession``: a context manager exposing
    ``.client`` (a connected ``AndroidDevice``) and ``screenshot() -> bytes``.
    """

    def __init__(self, *, serial: Optional[str] = None):
        self.client = None  # AndroidDevice once connected
        self._serial = serial

    def __enter__(self) -> "AndroidSession":
        # Lazy import keeps the package import-light and core.runtime.factory adapter-free.
        from gui_agent.adapters.android.device import AndroidDevice

        print("连接 Android 设备中 (adb)...")
        self.client = AndroidDevice(serial=self._serial)
        self.client.connect()
        print(f"设备连接成功 ({self.client.serial or 'auto'}), 分辨率 {self.client.win_w}x{self.client.win_h}")
        return self

    def __exit__(self, *_):
        if self.client:
            self.client.close()
        self.client = None

    def screenshot(self) -> bytes:
        if self.client is None:
            raise RuntimeError("Android 设备尚未连接")
        return self.client.screenshot()


class AndroidPerception:
    """Capture the current screen through an active android session."""

    def __init__(self, session: AndroidSession, screenshot_path: Path = SCREENSHOT):
        self.session = session
        self.screenshot_path = screenshot_path

    def observe(self) -> Observation:
        print("截图中 (Android)...")
        png_bytes = self.session.screenshot()
        # Downscale to the configured width (default 320) — cuts LLM tokens; tap
        # coords are unaffected (the executor denormalizes against device pixels).
        png_bytes = _downscale_width(png_bytes, SCREENSHOT_MAX_WIDTH)
        self.screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        self.screenshot_path.write_bytes(png_bytes)
        print(f"截图大小: {len(png_bytes) // 1024} KB，已保存到 {self.screenshot_path}")
        return Observation(png_bytes=png_bytes, source="android")
