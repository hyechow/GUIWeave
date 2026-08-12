"""iPhone Mirroring session and visual perception."""

from __future__ import annotations

from pathlib import Path
from gui_agent.adapters.iphone.client import (
    MirrorDaemonClient,
    SCKScreenshotClient,
)
from gui_agent.core.schemas import Observation


ROOT = Path(__file__).resolve().parents[3]
SCREENSHOT = ROOT / "logs" / "gui_agent" / "scratch" / "iphone_screenshot.png"
class IPhoneSession:
    """Use mirror_daemon for input and sck_server as the only screenshot source."""

    def __init__(self) -> None:
        self.client: MirrorDaemonClient | None = None
        self._sck: SCKScreenshotClient | None = None

    def __enter__(self) -> "IPhoneSession":
        print("启动 iPhone 截图流 (sck_server)...")
        sck = SCKScreenshotClient()
        sck.start()
        self._sck = sck
        try:
            print("连接 iPhone 输入后端 (mirror_daemon)...")
            client = MirrorDaemonClient()
            client.connect()
            self.client = client
        except Exception:
            sck.close()
            self._sck = None
            raise
        print("iPhone 已连接：sck_server 截图，mirror_daemon 输入")
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._sck is not None:
            self._sck.close()
            self._sck = None
        if self.client is not None:
            self.client.close()
            self.client = None

    def screenshot(self) -> bytes:
        if self._sck is None:
            raise RuntimeError("iPhone sck_server 尚未连接")
        return self._sck.screenshot()

    def platform_time(self):
        from gui_agent.core.runtime.clock import host_time_fallback

        return host_time_fallback(
            "iphone",
            reason=(
                "iPhone mirror_daemon/sck_server expose pixels and input only; "
                "no device clock channel is available"
            ),
        )


class IPhonePerception:
    def __init__(
        self,
        session: IPhoneSession,
        screenshot_path: Path = SCREENSHOT,
    ) -> None:
        self.session = session
        self.screenshot_path = screenshot_path

    def observe(self) -> Observation:
        png_bytes = self.session.screenshot()
        self.screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        self.screenshot_path.write_bytes(png_bytes)
        return Observation(png_bytes=png_bytes, source="iphone-mirror")


__all__ = ["IPhonePerception", "IPhoneSession"]
