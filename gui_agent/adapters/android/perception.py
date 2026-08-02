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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from gui_agent.adapters.android.accessibility import (
    collection_regions_from_uiautomator,
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

    def capture(self) -> tuple[bytes, str | None]:
        """Capture both sensors concurrently; only screenshot failure is fatal."""
        if self.client is None:
            raise RuntimeError("Android 设备尚未连接")
        with ThreadPoolExecutor(max_workers=2) as pool:
            screenshot = pool.submit(self.client.screenshot)
            hierarchy = pool.submit(self.client.dump_ui_hierarchy)
            png_bytes = screenshot.result()
            xml_text = hierarchy.result()
        # UIAutomator is optional but occasionally returns no XML while pixels are
        # already stable. Retry only that missing channel; successful frames keep the
        # concurrent one-shot cost and the required screenshot is never recaptured.
        if xml_text is None:
            xml_text = self.client.dump_ui_hierarchy()
        return png_bytes, xml_text


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
        )
