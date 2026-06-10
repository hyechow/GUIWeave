"""adbutils I/O backend implementing gui_agent.core.contracts.Device.

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
    DEFAULT_SERIAL,
    KEYCODE,
    SCROLL_PX_PER_AMOUNT,
    VENDORED_ADB,
)


def _clamp(v: float, lo: float, hi: float) -> int:
    return int(max(lo, min(v, hi)))


class AndroidDevice:
    """adb-backed phone device. Vision-only; no UIAutomator tree access here."""

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
        return self

    def close(self):
        """Drop the device handle WITHOUT killing the adb server."""
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

    # ----- input primitives (Device Protocol) ------------------------------
    def tap(self, x: float, y: float) -> str:
        try:
            self._require_dev().click(round(x), round(y))
        except Exception as exc:  # noqa: BLE001 — surface as status str for executor
            return f"failed: {exc}"
        return f"OK tap ({x:.0f},{y:.0f})"

    def type_text(self, text: str) -> str:
        """Type ASCII text via ``input text`` (spaces -> %s). Non-ASCII unsupported."""
        if not text.isascii():
            return "failed: non-ascii text not supported yet (TODO: IME/clipboard)"
        try:
            # `input text` treats a literal space specially; %s encodes a space.
            self._require_dev().shell(["input", "text", text.replace(" ", "%s")])
        except Exception as exc:  # noqa: BLE001
            return f"failed: {exc}"
        return f"OK type {text!r}"

    def clear_text(self) -> str:
        """Best-effort clear of the focused field: MOVE_END then a bounded run of
        DEL (one shell call). Clears up to ~60 chars — enough for search boxes."""
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
        Endpoints are clamped inside the screen.
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
        fx, tx = _clamp(fx, 5, w - 5), _clamp(tx, 5, w - 5)
        fy, ty = _clamp(fy, 5, h - 5), _clamp(ty, 5, h - 5)
        try:
            self._require_dev().swipe(fx, fy, tx, ty, 0.3)
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
