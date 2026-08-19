"""Android perception: session lifecycle + observe(), mirroring the iphone shape.

``AndroidSession`` is the neutral perception/lifecycle surface core holds as
``phone`` (it satisfies ``PerceptionSession``): a context manager that connects an
:class:`AndroidDevice` on ``__enter__``, exposes ``.client`` + ``screenshot()`` and
drops the handle on ``__exit__`` (without killing the adb server).

``AndroidPerception`` captures required pixels plus an optional UIAutomator tree.
Hierarchy failures degrade to the screenshot-only path.
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
        self.last_capture_timing: dict[str, float | int | bool] = {}

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

    def settle_screenshot(self) -> bytes:
        """Capture one lightweight frame for best-effort post-action settling."""

        if self.client is None:
            raise RuntimeError("Android 设备尚未连接")
        return self.client.screenshot_once()

    def platform_time(self):
        if self.client is None:
            raise RuntimeError("Android 设备尚未连接")
        return self.client.platform_time()

    def capture(self) -> tuple[bytes, str | None]:
        """Capture pixels only (UIAutomator structured perception is removed)."""
        if self.client is None:
            raise RuntimeError("Android 设备尚未连接")
        # The UIAutomator sensor is gone: it cost 2-6s per frame, returned an opaque
        # shell for WebView apps, and could lag the screenshot (an old hierarchy
        # paired with a new frame). Screenshot is authoritative; observe() still
        # tries the optional WebView CDP document as a structured channel.
        png_bytes = self.client.screenshot()
        self.last_capture_timing = {
            "hierarchy_seconds": 0.0,
            "hierarchy_available": False,
            "screenshot_seconds": 0.0,
        }
        return png_bytes, None

    def list_apps(self) -> list[str]:
        """Return launchable application names from the active package manager."""
        if self.client is None:
            raise RuntimeError("Android 设备尚未连接")
        return self.client.list_apps()


class AndroidPerception:
    """Capture the current screen through an active android session."""

    def __init__(self, session: AndroidSession, screenshot_path: Path = SCREENSHOT):
        self.session = session
        self.screenshot_path = screenshot_path

    def observe(self) -> Observation:
        print("截图中 (Android)...")
        png_bytes, hierarchy = self.session.capture()
        client = self.session.client
        # UIAutomator structured perception is removed: it costs 2-6s per frame,
        # returns an opaque shell for WebView apps, and can lag the screenshot (an
        # old hierarchy paired with a new frame). The screenshot is authoritative;
        # the WebView CDP document remains as an optional structured channel.
        webview = (
            client.webview_document()
            if client is not None
            and (not hierarchy or "android.webkit.WebView" in hierarchy)
            else None
        )
        # Downscale to the configured width; tap coordinates are unaffected because
        # the executor denormalizes against device pixels.
        png_bytes = _downscale_width(png_bytes, SCREENSHOT_MAX_WIDTH)
        self.screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        self.screenshot_path.write_bytes(png_bytes)
        print(f"截图大小: {len(png_bytes) // 1024} KB，已保存到 {self.screenshot_path}")
        return Observation(
            png_bytes=png_bytes,
            source="android",
            url=(webview or {}).get("url") or None,
            # CDP's document title is the authoritative surface identity when the
            # guarded WebView sensor succeeded.
            title=(webview or {}).get("title") or None,
            semantic_tree=None,
            tables=(webview or {}).get("tables", []),
            collection_regions=[],
            form_control_state=None,
        )
