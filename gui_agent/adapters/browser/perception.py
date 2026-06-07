"""Browser perception: session lifecycle + observe(), mirroring the iphone shape.

``BrowserSession`` is the neutral perception/lifecycle surface core holds as
``phone`` (it satisfies ``PerceptionSession``): a context manager that connects a
:class:`PlaywrightDevice` on ``__enter__``, exposes ``.client`` + ``screenshot()``
and detaches on ``__exit__`` (without closing the user's Chrome).

``BrowserPerception`` wraps the session's screenshot in an ``Observation`` with
``source='browser'`` (satisfies ``Perception``). VISION-ONLY: pixels only, no
element_tree / page_key — exactly like ``LivePerception``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from gui_agent.core.schemas import Observation

ROOT = Path(__file__).resolve().parents[3]
SCREENSHOT = ROOT / "logs" / "gui_agent" / "scratch" / "browser_screenshot.png"


class BrowserSession:
    """Owns the active CDP connection + screenshot source for the agent loop.

    Mirrors ``LivePhoneSession``: a context manager exposing ``.client``
    (a connected ``PlaywrightDevice``) and ``screenshot() -> bytes``.
    """

    def __init__(
        self,
        *,
        cdp_url: Optional[str] = None,
        start_url: Optional[str] = None,
    ):
        self.client = None  # PlaywrightDevice once connected
        self._cdp_url = cdp_url
        self._start_url = start_url

    def __enter__(self) -> "BrowserSession":
        # Lazy import keeps the package import-light and core.factory adapter-free.
        from gui_agent.adapters.browser.device import PlaywrightDevice

        print("连接浏览器中 (Chrome CDP)...")
        self.client = PlaywrightDevice(cdp_url=self._cdp_url, start_url=self._start_url)
        self.client.connect()
        print("浏览器连接成功")
        return self

    def __exit__(self, *_):
        if self.client:
            self.client.close()
        self.client = None

    def screenshot(self) -> bytes:
        if self.client is None:
            raise RuntimeError("浏览器尚未连接")
        return self.client.screenshot()


class BrowserPerception:
    """Capture the current page through an active browser session."""

    def __init__(self, session: BrowserSession, screenshot_path: Path = SCREENSHOT):
        self.session = session
        self.screenshot_path = screenshot_path

    def observe(self) -> Observation:
        print("截图中 (浏览器)...")
        png_bytes = self.session.screenshot()
        self.screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        self.screenshot_path.write_bytes(png_bytes)
        print(f"截图大小: {len(png_bytes) // 1024} KB，已保存到 {self.screenshot_path}")
        return Observation(png_bytes=png_bytes, source="browser")
