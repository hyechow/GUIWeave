"""adbutils I/O backend implementing gui_agent.core.runtime.contracts.Device.

``AndroidDevice`` drives a real Android (or HarmonyOS AOSP-compat) phone over adb:
``screencap`` for frames, ``input tap/swipe/text/keyevent`` for control. It
satisfies the neutral ``Device`` Protocol (connect/close/screenshot/tap/type_text/
drag/press_home) AND the optional ``ScrollableDevice`` capability (scroll), plus
the android-only extras ``back()`` / ``app_switch()`` / ``key(code)`` / ``long_press``.

COORDINATES — THE KEY SIMPLIFICATION vs iPhone
----------------------------------------------
``screencap`` is the device's own physical pixels and ``input`` consumes the SAME
pixel space, so there is NO mirror-window geometry: the executor denormalizes
0-1000 -> device px via ``viewport_size`` (the cached ``window_size()``), and that
pixel goes straight to ``input tap``. No window origin, no retina /2, no Quartz
screen mapping (all of iphone's ``logical_xy`` / WIN_W / ``_find_iphone_window``
disappears).

Every input method returns a status ``str`` (the executor scans it for
"paused" / "interrupted" / "failed"). ``close()`` drops the device handle WITHOUT
killing the adb server (the wireless/USB transport stays up for the next run).

Input is injected on-device by adb, so it is zero-preempt by nature (it never
touches the Mac cursor) — but it is NOT the iphone daemon path, so
``zero_preempt = False`` (core then takes the ordinary input branch).
"""

from __future__ import annotations

import io
import os
from typing import Optional

from gui_agent.adapters.android.constants import (
    ADBKEYBOARD_IME,
    ADBKEYBOARD_PKG,
    DEFAULT_SERIAL,
    KEYCODE,
    SCROLL_PX_PER_AMOUNT,
    VENDORED_ADB,
)


def _clamp(v: float, lo: float, hi: float) -> int:
    return int(max(lo, min(v, hi)))


class AndroidDevice:
    """adb-backed phone device. Vision-only PERCEPTION (screenshots only); the
    UIAutomator a11y tree is exposed solely via the optional ``read_visible_text()``
    read-action (ScreenTextReader capability) — NOT a perception channel: probed on
    demand and viewport-filtered, so it reads exactly what is on screen."""

    # Capability marker probed by core via getattr(client, "zero_preempt", False).
    # adb input injects on-device (never steals the Mac cursor), but it is not the
    # iphone zero-preempt DAEMON path, so core should take the ordinary branch.
    zero_preempt = False

    def __init__(self, serial: Optional[str] = None):
        self.serial = serial or DEFAULT_SERIAL
        self._dev = None  # adbutils.AdbDevice once connected
        # Cached physical resolution (the executor denormalizes against these).
        # Defaults are overwritten by window_size() on connect().
        self.win_w = 1080
        self.win_h = 2400
        self._adbkeyboard_active = False  # set by _detect_ime() at connect when ADBKeyboard is on

    # ----- lifecycle -------------------------------------------------------
    def connect(self):
        """Resolve the adb binary, attach to the device, wake it, cache resolution.

        ``serial`` semantics: ``host:port`` -> wireless (``adb connect`` first);
        a bare USB serial -> direct; ``None`` -> auto-select the sole device.
        """
        import adbutils

        # adbutils wheel ships no adb binary. Point it at the bundled standalone
        # adb only when the user has not set ADBUTILS_ADB_PATH and the binary
        # exists — otherwise respect their env / PATH.
        if not os.environ.get("ADBUTILS_ADB_PATH") and VENDORED_ADB.exists():
            os.environ["ADBUTILS_ADB_PATH"] = str(VENDORED_ADB)

        client = adbutils.AdbClient()
        serial = self.serial
        if serial:
            if ":" in serial:  # wireless host:port — ensure the transport exists
                try:
                    client.connect(serial, timeout=5.0)
                except Exception:  # noqa: BLE001 — may already be connected
                    pass
            self._dev = client.device(serial)
        else:
            devices = client.device_list()
            if not devices:
                raise RuntimeError(
                    "no adb device found (set ANDROID_SERIAL, or `adb connect <ip:port>`)"
                )
            if len(devices) > 1:
                serials = ", ".join(d.serial for d in devices)
                raise RuntimeError(f"multiple adb devices; set ANDROID_SERIAL ({serials})")
            self._dev = devices[0]

        # screencap returns black when the screen is off — wake it on connect.
        try:
            self._dev.keyevent(KEYCODE["wakeup"])
        except Exception:  # noqa: BLE001 — non-fatal
            pass
        try:
            ws = self._dev.window_size()
            self.win_w, self.win_h = int(ws[0]), int(ws[1])
        except Exception:  # noqa: BLE001 — keep defaults
            pass
        self._detect_ime()
        return self

    def _detect_ime(self) -> None:
        """Per-connect path — READ-ONLY: record whether the device's current IME is
        ADBKeyboard (sets ``_adbkeyboard_active``, which gates non-ASCII type_text). It
        does NOT switch the IME: switching is one-time environment setup done in
        ``ensure_adbkeyboard`` (called from the platform setup_check), kept out of the
        execution path. So connect only OBSERVES; if setup_check already switched the
        device to ADBKeyboard, this sees it and enables Chinese typing."""
        try:
            cur = self._dev.shell("settings get secure default_input_method").strip()
        except Exception:  # noqa: BLE001
            return
        self._adbkeyboard_active = cur == ADBKEYBOARD_IME

    def ensure_adbkeyboard(self) -> tuple[bool, bool]:
        """Environment SETUP (called from the android setup_check, NOT the per-connect
        path): if ADBKeyboard is installed, switch the device IME to it so the session
        can type non-ASCII (Chinese) and no soft keyboard occludes screenshots. The
        switch persists device-side across sessions (deliberately not restored). Must run
        BEFORE any field is focused (switching the IME defocuses the active field), which
        setup_check guarantees by running before the session opens.

        Returns ``(installed, active)``: ``installed`` whether the ADBKeyboard package is
        present, ``active`` whether the IME is now ADBKeyboard (verified by re-read)."""
        dev = self._require_dev()
        try:
            installed = ADBKEYBOARD_PKG in dev.shell(f"pm list packages {ADBKEYBOARD_PKG}")
        except Exception:  # noqa: BLE001
            return (False, False)
        if not installed:
            self._adbkeyboard_active = False
            return (False, False)
        try:
            cur = dev.shell("settings get secure default_input_method").strip()
            if cur != ADBKEYBOARD_IME:
                dev.shell(f"ime enable {ADBKEYBOARD_IME}")
                dev.shell(f"ime set {ADBKEYBOARD_IME}")
            # Verify — `ime set` on a missing/invalid IME fails silently.
            now = dev.shell("settings get secure default_input_method").strip()
        except Exception:  # noqa: BLE001
            return (True, False)
        self._adbkeyboard_active = now == ADBKEYBOARD_IME
        return (True, self._adbkeyboard_active)

    def close(self):
        """Drop the device handle WITHOUT killing the adb server. The ADBKeyboard IME
        is intentionally left active (not restored)."""
        self._adbkeyboard_active = False
        self._dev = None

    # ----- viewport (for the executor's denormalization) -------------------
    @property
    def viewport_size(self) -> tuple[int, int]:
        """(width, height) in device pixels the executor uses to denormalize 0-1000."""
        return self.win_w, self.win_h

    # ----- perception ------------------------------------------------------
    def screenshot(self) -> bytes:
        """Return the current screen as PNG bytes.

        Primary path: ``adbutils.screenshot()`` (PIL.Image, ~= exec-out screencap).
        Guards against the occasional empty/0-byte exec-out frame by retrying and
        falling back to the file method (``screencap -p /sdcard/... && pull``).
        """
        last_exc: Optional[Exception] = None
        for _ in range(3):
            try:
                img = self._require_dev().screenshot()
                if img is not None:
                    buf = io.BytesIO()
                    img.save(buf, format="PNG")
                    data = buf.getvalue()
                    if len(data) > 100:  # reject empty/near-empty frames
                        return data
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
            try:
                data = self._screencap_pull()
                if len(data) > 100:
                    return data
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
        raise RuntimeError(f"screenshot failed after retries: {last_exc}")

    def _screencap_pull(self) -> bytes:
        """File-based capture: screencap to /sdcard then pull (robust fallback)."""
        import tempfile

        dev = self._require_dev()
        remote = "/sdcard/_gui_agent_shot.png"
        dev.shell(f"screencap -p {remote}")
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
            local = tf.name
        try:
            dev.sync.pull(remote, local)
            with open(local, "rb") as fh:
                return fh.read()
        finally:
            try:
                os.unlink(local)
            except OSError:
                pass
            try:
                dev.shell(f"rm -f {remote}")
            except Exception:  # noqa: BLE001
                pass

    def read_visible_text(self) -> str:
        """Read all VISIBLE text on the current frame as one newline-joined string
        (the optional ScreenTextReader capability).

        Source: ``uiautomator dump`` (the Android a11y tree), taking each node's
        ``text`` + ``content-desc``. Viewport-bounds-filtered against ``win_w`` /
        ``win_h`` so the agent reads EXACTLY what is on screen (no off-screen,
        scroll-to-reveal nodes) — this is what keeps it a non-perception read-action
        rather than a sneakier super-perception: read == seen.

        NON-INTERACTIVE: uiautomator dump does not tap/scroll/change the UI. Empty
        string when the dump is unreadable (caller falls back to the vision read)."""
        import re
        import xml.etree.ElementTree as ET
        from html import unescape

        dev = self._require_dev()
        remote = "/sdcard/_gui_agent_ui.xml"
        xml_text = ""
        for _ in range(2):  # uiautomator dump occasionally fails first try mid-transition
            try:
                dev.shell(f"uiautomator dump {remote}")
                xml_text = dev.shell(f"cat {remote}")
            except Exception:  # noqa: BLE001
                xml_text = ""
            if "<node" in xml_text:
                break
            xml_text = ""
        try:
            dev.shell(f"rm -f {remote}")
        except Exception:  # noqa: BLE001
            pass
        if not xml_text:
            return ""

        w, h = self.win_w, self.win_h
        bounds_re = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
        try:
            root = ET.fromstring(xml_text)
        except Exception:  # noqa: BLE001
            return ""
        pieces: list[str] = []
        for node in root.iter("node"):
            bm = bounds_re.search(node.get("bounds") or "")
            if not bm:
                continue
            x1, y1, x2, y2 = (int(bm.group(i)) for i in (1, 2, 3, 4))
            # keep nodes INTERSECTING the viewport (partly visible == visible)
            if x2 <= 0 or y2 <= 0 or x1 >= w or y1 >= h:
                continue
            for attr in ("text", "content-desc"):
                val = (node.get(attr) or "").strip()
                if val:
                    pieces.append(unescape(val))
        return "\n".join(pieces)

    # ----- input primitives (Device Protocol) ------------------------------
    def tap(self, x: float, y: float) -> str:
        try:
            self._require_dev().click(round(x), round(y))
        except Exception as exc:  # noqa: BLE001 — surface as status str for executor
            return f"failed: {exc}"
        return f"OK tap ({x:.0f},{y:.0f})"

    def type_text(self, text: str) -> str:
        """Type into the focused field via the ADBKeyboard IME — the SINGLE input path
        for android. It handles every character (ASCII, Chinese, symbols, emoji) the
        same way, so there is no ASCII-vs-non-ASCII split: the text is base64-encoded
        and sent with ``am broadcast -a ADB_INPUT_B64``, inserted at the cursor (the
        executor clears the field first). ADBKeyboard is switched on once in setup_check;
        ``_adbkeyboard_active`` (set by _detect_ime at connect) gates this."""
        if not self._adbkeyboard_active:
            return "failed: 需 ADBKeyboard 输入法（setup_check 未就绪 / 未安装）"
        import base64

        b64 = base64.b64encode(text.encode("utf-8")).decode()
        try:
            self._require_dev().shell(["am", "broadcast", "-a", "ADB_INPUT_B64", "--es", "msg", b64])
        except Exception as exc:  # noqa: BLE001
            return f"failed: {exc}"
        return f"OK type {text!r}"

    def clear_text(self) -> str:
        """Clear the focused field. With ADBKeyboard active (the single input path) use
        its native ``ADB_CLEAR_TEXT`` broadcast — one call, clears the whole field
        regardless of length. Fallback when ADBKeyboard is not ready: MOVE_END + a
        bounded run of DEL (~60 chars)."""
        if self._adbkeyboard_active:
            try:
                self._require_dev().shell(["am", "broadcast", "-a", "ADB_CLEAR_TEXT"])
            except Exception as exc:  # noqa: BLE001
                return f"failed: {exc}"
            return "OK clear"
        codes = " ".join([str(KEYCODE["move_end"])] + [str(KEYCODE["del"])] * 60)
        try:
            self._require_dev().shell(f"input keyevent {codes}")
        except Exception as exc:  # noqa: BLE001
            return f"failed: {exc}"
        return "OK clear"

    def press_enter(self) -> str:
        return self.key(KEYCODE["enter"])

    def scroll(
        self,
        direction: str,
        amount: int = 5,
        x: float = 540,
        y: float = 1200,
    ) -> str:
        """Swipe-simulated wheel scroll (ScrollableDevice). adb has no wheel.

        Content semantics: down=see content below (finger swipes up); up=see above;
        right=see content to the right (finger swipes left); left=see content left.

        Vertical endpoints are kept INSIDE a system-gesture-safe band (away from the very
        top / bottom edges): a swipe starting at the top edge pulls down the HarmonyOS
        notification shade / Control Center, and one at the bottom edge triggers nav
        gestures — an edge-anchored picker scroll otherwise derails the whole task.
        """
        dist = max(1, int(amount)) * SCROLL_PX_PER_AMOUNT
        half = dist // 2
        cx, cy = round(x), round(y)
        d = (direction or "").strip().lower()
        if d in ("down", "向下", "downward"):
            fx, fy, tx, ty = cx, cy + half, cx, cy - half
        elif d in ("up", "向上", "upward"):
            fx, fy, tx, ty = cx, cy - half, cx, cy + half
        elif d in ("right", "向右", "rightward"):
            fx, fy, tx, ty = cx + half, cy, cx - half, cy
        elif d in ("left", "向左", "leftward"):
            fx, fy, tx, ty = cx - half, cy, cx + half, cy
        else:
            return f"failed: unknown scroll direction {direction!r}"
        w, h = self.win_w, self.win_h
        v_inset = max(120, h // 12)  # keep off the top/bottom OS-gesture zones
        fx, tx = _clamp(fx, 5, w - 5), _clamp(tx, 5, w - 5)
        fy, ty = _clamp(fy, v_inset, h - v_inset), _clamp(ty, v_inset, h - v_inset)
        # Swipe SPEED matters for wheel pickers: a fast flick flings past the target by
        # momentum. Keep 1-2 unit picker gestures slow and bounded 3-5 unit picker
        # coarse moves at medium speed; ordinary list large still flings via 8 units.
        secs = 0.7 if amount <= 2 else 0.45 if amount <= 5 else 0.25
        try:
            self._require_dev().swipe(fx, fy, tx, ty, secs)
        except Exception as exc:  # noqa: BLE001
            return f"failed: {exc}"
        return f"OK scroll {d} {dist}px ({fx},{fy})->({tx},{ty})"

    def drag(
        self,
        from_x: float,
        from_y: float,
        to_x: float,
        to_y: float,
        duration_ms: int = 1000,
        cursor_mode: str | None = None,
    ) -> str:
        """Touch drag via ``input swipe`` with a duration. ``cursor_mode`` ignored
        (no Mac cursor involved — input is injected on-device)."""
        secs = max(0.05, (duration_ms or 1000) / 1000.0)
        try:
            self._require_dev().swipe(round(from_x), round(from_y), round(to_x), round(to_y), secs)
        except Exception as exc:  # noqa: BLE001
            return f"failed: {exc}"
        return f"OK drag ({from_x:.0f},{from_y:.0f})->({to_x:.0f},{to_y:.0f})"

    def press_home(self) -> str:
        """Go to the launcher (system Home key)."""
        return self.key(KEYCODE["home"])

    # ----- android-only extras (executor / manual use; not Device Protocol) -
    def back(self) -> str:
        """System back (KEYCODE_BACK)."""
        return self.key(KEYCODE["back"])

    def app_switch(self) -> str:
        """App switcher / recents / multitask view (KEYCODE_APP_SWITCH)."""
        return self.key(KEYCODE["app_switch"])

    def wake(self) -> str:
        return self.key(KEYCODE["wakeup"])

    def long_press(self, x: float, y: float, duration_ms: int = 600) -> str:
        """Long press = an in-place swipe of the given duration."""
        return self.drag(x, y, x, y, duration_ms)

    def key(self, code: int) -> str:
        """Generic keyevent out (the one place keyevents are issued)."""
        try:
            self._require_dev().keyevent(int(code))
        except Exception as exc:  # noqa: BLE001
            return f"failed: {exc}"
        return f"OK key {code}"

    # ----- internals -------------------------------------------------------
    def _require_dev(self):
        if self._dev is None:
            raise RuntimeError("AndroidDevice 尚未连接（先调用 connect()）")
        return self._dev
