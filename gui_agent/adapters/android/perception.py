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
import time
from pathlib import Path
from typing import Optional

from gui_agent.adapters.android.accessibility import (
    collection_regions_from_uiautomator,
    form_controls_from_semantic_tree,
    semantic_tree_from_uiautomator,
)
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
        """Capture a hierarchy followed by pixels from the same settled UI state."""
        if self.client is None:
            raise RuntimeError("Android 设备尚未连接")
        # UIAutomator is slower than screencap. Taking them concurrently can pair an
        # old transition frame with a hierarchy from the destination screen. Finish
        # the optional structural sensor first, then capture the authoritative pixels.
        hierarchy_started_at = time.perf_counter()
        # This sensor is optional. A failed/slow dump must not be repeated inside
        # one observation; screenshot perception remains authoritative and the
        # next real turn will try UIAutomator again.
        xml_text = self.client.dump_ui_hierarchy(timeout_s=3.0)
        hierarchy_seconds = time.perf_counter() - hierarchy_started_at
        screenshot_started_at = time.perf_counter()
        png_bytes = self.client.screenshot()
        screenshot_seconds = time.perf_counter() - screenshot_started_at
        self.last_capture_timing = {
            "hierarchy_seconds": round(hierarchy_seconds, 3),
            "hierarchy_available": xml_text is not None,
            "screenshot_seconds": round(screenshot_seconds, 3),
        }
        return png_bytes, xml_text

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
        semantic_tree = semantic_tree_from_uiautomator(
            hierarchy,
            viewport_size=client.viewport_size if client is not None else (0, 0),
        )
        collection_regions = collection_regions_from_uiautomator(
            hierarchy,
            viewport_size=client.viewport_size if client is not None else (0, 0),
        )
        form_controls = form_controls_from_semantic_tree(semantic_tree)
        # Downscale to the configured width; tap coordinates are unaffected because
        # the executor denormalizes against device pixels.
        png_bytes = _downscale_width(png_bytes, SCREENSHOT_MAX_WIDTH)
        self.screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        self.screenshot_path.write_bytes(png_bytes)
        print(f"截图大小: {len(png_bytes) // 1024} KB，已保存到 {self.screenshot_path}")
        if semantic_tree is not None:
            print(f"结构节点: {len(semantic_tree)}（UIAutomator）")
        return Observation(
            png_bytes=png_bytes,
            source="android",
            semantic_tree=semantic_tree,
            collection_regions=collection_regions,
            form_control_state=form_controls,
        )
