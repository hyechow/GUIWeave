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

from pathlib import Path
from typing import Optional

from gui_agent.core.schemas import Observation

ROOT = Path(__file__).resolve().parents[3]
SCREENSHOT = ROOT / "logs" / "gui_agent" / "scratch" / "android_screenshot.png"


class AndroidSession:
    """Owns the active adb device + screenshot source for the agent loop.

    Mirrors ``LivePhoneSession`` / ``BrowserSession``: a context manager exposing
    ``.client`` (a connected ``AndroidDevice``) and ``screenshot() -> bytes``.
    """

    def __init__(self, *, serial: Optional[str] = None):
        self.client = None  # AndroidDevice once connected
        self._serial = serial

    def __enter__(self) -> "AndroidSession":
        # Lazy import keeps the package import-light and core.factory adapter-free.
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
        self.screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        self.screenshot_path.write_bytes(png_bytes)
        print(f"截图大小: {len(png_bytes) // 1024} KB，已保存到 {self.screenshot_path}")
        return Observation(png_bytes=png_bytes, source="android")
