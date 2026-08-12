"""Browser perception: session lifecycle + observe(), mirroring the iphone shape.

``BrowserSession`` is the neutral perception/lifecycle surface core holds as
``phone`` (it satisfies ``PerceptionSession``): a context manager that connects
or launches a :class:`PlaywrightDevice` on ``__enter__``, exposes ``.client`` +
``screenshot()``, and detaches/closes on ``__exit__``.

``BrowserPerception`` wraps the session's screenshot in an ``Observation`` with
``source='browser'`` (satisfies ``Perception``). The primary observation remains
the screenshot; browser-only structural sensors add URL/title/form/table metadata
when CDP can read them.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from gui_agent.adapters.browser.filter_state import typed_applied_filter_state
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
        headless: bool | None = None,
        user_data_dir: Optional[str] = None,
    ):
        from gui_agent.adapters.browser.factory import _resolve_headless

        self.client = None  # PlaywrightDevice once connected
        self._cdp_url = cdp_url
        self._start_url = start_url
        self._headless = _resolve_headless(headless)
        self._user_data_dir = user_data_dir or os.environ.get("BROWSER_USER_DATA_DIR")

    def __enter__(self) -> "BrowserSession":
        # Lazy import keeps the package import-light and core.runtime.factory adapter-free.
        from gui_agent.adapters.browser.device import PlaywrightDevice

        if self._headless:
            print("启动浏览器中 (headless Chromium)...")
        else:
            print("连接浏览器中 (Chrome CDP)...")
        self.client = PlaywrightDevice(
            cdp_url=self._cdp_url,
            start_url=self._start_url,
            headless=self._headless,
            user_data_dir=self._user_data_dir,
        )
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

    def platform_time(self):
        if self.client is None:
            raise RuntimeError("浏览器尚未连接")
        return self.client.platform_time()

    def pop_tab_switched(self) -> bool:
        """Delegate to PlaywrightDevice: True (and clear) if a tab switch occurred."""
        if self.client is None:
            return False
        pop = getattr(self.client, "pop_tab_switched", None)
        return bool(pop()) if pop is not None else False

    def wait_settled(self, action_type=None):
        """Delegate to PlaywrightDevice's CDP-based settle (readyState + DOM-mutation + network
        quiet). The runner's _settle_after_action prefers this over pixel-diff when present."""
        if self.client is None:
            raise RuntimeError("浏览器尚未连接")
        return self.client.wait_settled(action_type)

    def read_tables(self) -> list[dict]:
        """Delegate to the browser's read-only DOM table snapshot sensor."""
        if self.client is None:
            return []
        read = getattr(self.client, "read_tables", None)
        return read() if read is not None else []

    def read_form_controls(self) -> list[dict]:
        """Delegate to the browser's read-only DOM form-control sensor."""
        if self.client is None:
            return []
        read = getattr(self.client, "read_form_controls", None)
        return read() if read is not None else []


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
        # Structural loading signal (document.readyState) for the supervisor — far
        # more reliable than a pixel blank-screen guess on desktop web, where a
        # rendered empty page is mostly whitespace. None-safe if the device lacks it.
        client = getattr(self.session, "client", None)
        loading = bool(client.is_loading()) if (client is not None and hasattr(client, "is_loading")) else None
        # Structured page metadata the vision-only screenshot can't show (no address bar / tab
        # title): hand the checker the real URL/title so it judges page identity from ground
        # truth instead of fabricating it.
        url = title = None
        if client is not None and hasattr(client, "page_info"):
            url, title = client.page_info()
        # Interactive-state fingerprint (form values / checked state): the structural progress
        # signal for form filling, where pixels barely change and planner instructions
        # read alike — suppresses false stuck/repetition (see policy DOMChanged).
        dom_state = None
        if client is not None and hasattr(client, "form_state_fingerprint"):
            dom_state = client.form_state_fingerprint()
        tables = []
        viewport = None
        if client is not None and hasattr(client, "read_tables"):
            tables = client.read_tables()
            if hasattr(client, "read_viewport"):
                viewport = client.read_viewport()
        form_controls = []
        form_controls_meta = None
        form_control_state = []
        form_control_state_meta = None
        if client is not None and hasattr(client, "read_form_controls"):
            form_controls = client.read_form_controls()
            if hasattr(client, "read_form_controls_meta"):
                form_controls_meta = client.read_form_controls_meta()
            if hasattr(client, "read_form_control_state"):
                form_control_state = client.read_form_control_state()
            if hasattr(client, "read_form_control_state_meta"):
                form_control_state_meta = client.read_form_control_state_meta()
        semantic_tree = None
        if client is not None and hasattr(client, "read_semantic_tree"):
            semantic_tree = client.read_semantic_tree() or None
        applied_filters = None
        applied_filter_meta = None
        if client is not None and hasattr(client, "read_applied_filter_state"):
            applied_filters, applied_filter_meta = client.read_applied_filter_state()
        elif client is not None and hasattr(client, "read_applied_filters"):
            applied_filters = client.read_applied_filters()
        applied_filter_state = typed_applied_filter_state(
            applied_filters,
            applied_filter_meta,
        )
        return Observation(
            png_bytes=png_bytes, source="browser", loading=loading,
            url=url or None, title=title or None, dom_state=dom_state,
            tables=tables or None,
            form_controls=form_controls or None,
            form_controls_meta=form_controls_meta,
            form_control_state=form_control_state or None,
            form_control_state_meta=form_control_state_meta,
            viewport=viewport or None,
            semantic_tree=semantic_tree,
            applied_filters=applied_filters or None,
            applied_filter_state=applied_filter_state,
            applied_filter_meta=applied_filter_meta or None,
        )
