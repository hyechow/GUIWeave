"""Android adapter constants: serial resolution, scroll units, key codes.

NO ``WIN_W`` / ``WIN_H`` here (unlike the iphone ``executor_constants``): adb
``screencap`` is the device's own physical resolution and ``adb shell input``
consumes that same pixel space, so there is no mirror-window geometry. The live
resolution is read once per connection via ``window_size()`` and cached on the
device, not hard-coded.
"""

from __future__ import annotations

import os
from pathlib import Path

# Repo root: adapters/android/constants.py -> parents[3] == repo root (same depth
# the browser adapter assumes for ROOT in perception.py).
_REPO_ROOT = Path(__file__).resolve().parents[3]

# The plugin launcher injects its bundled standalone adb through
# GUIWEAVE_ADB_PATH. A source-checkout Console does not pass through that
# launcher, so it must also discover the exact same plugin asset directly.
# Keep the former repository vendor location as a compatibility fallback.
# The adbutils wheel itself ships no adb executable.
_configured_adb = os.environ.get("GUIWEAVE_ADB_PATH", "").strip()
_plugin_adb = (
    _REPO_ROOT
    / "plugins"
    / "guiweave-automation"
    / "assets"
    / "android-arm64"
    / "scrcpy-macos-aarch64-v4.0"
    / "adb"
)
_legacy_vendor_adb = _REPO_ROOT / "vendor" / "scrcpy-macos-aarch64-v4.0" / "adb"
VENDORED_ADB = Path(_configured_adb).expanduser().resolve() if _configured_adb else (
    _plugin_adb if _plugin_adb.is_file() else _legacy_vendor_adb
)

# Target device serial: "host:port" for wireless adb (e.g. 192.168.31.240:5555)
# or a USB serial. None -> auto-select the sole connected device.
DEFAULT_SERIAL = os.environ.get("ANDROID_SERIAL") or None

# Swipe pixels per scroll unit. With AndroidExecutor's wider unit map (small/medium/
# large -> 1/4/8) this gives small≈140px (≈1 wheel-picker row on a 1080x2400 panel),
# medium≈560px (≈¼ screen), large≈1120px (≈½ screen). Tuned so a wheel/time picker can
# be nudged ~1 row at a time (small) instead of overshooting, while lists still fling.
SCROLL_PX_PER_AMOUNT = 140

# Downscale the captured screenshot to this WIDTH (px), preserving aspect, before it
# becomes the Observation — cuts LLM image tokens. Coordinates are UNAFFECTED: the
# executor denormalizes 0-1000 against the DEVICE resolution (window_size), not the
# image size. 0 disables. Override via env ANDROID_SCREENSHOT_WIDTH.
SCREENSHOT_MAX_WIDTH = int(os.environ.get("ANDROID_SCREENSHOT_WIDTH") or 320)

# ADBKeyboard IME (github.com/senzhk/ADBKeyBoard): a null keyboard that types text
# received via `am broadcast -a ADB_INPUT_B64 --es msg <base64>`. The ONLY reliable
# adb path for non-ASCII (Chinese) input — `input text` is ASCII-only and writing the
# clipboard from shell is blocked on Android 10+. The platform setup_check switches the
# device to it ONCE (and leaves it set; it shows no soft keyboard, so it never occludes
# screenshots). The per-connect path (_detect_ime) only OBSERVES the current IME.
ADBKEYBOARD_IME = "com.android.adbkeyboard/.AdbIME"
# Package id — used by the environment setup (AndroidDevice.ensure_adbkeyboard, called
# from setup_check) to check the IME is installed before switching. NOT used in the
# per-connect execution path.
ADBKEYBOARD_PKG = "com.android.adbkeyboard"

# Android key event codes (https://developer.android.com/reference/android/view/KeyEvent).
KEYCODE = {
    "home": 3,          # KEYCODE_HOME
    "back": 4,          # KEYCODE_BACK (system back)
    "enter": 66,        # KEYCODE_ENTER
    "del": 67,          # KEYCODE_DEL (backspace)
    "move_end": 123,    # KEYCODE_MOVE_END
    "app_switch": 187,  # KEYCODE_APP_SWITCH (recents / multitask)
    "wakeup": 224,      # KEYCODE_WAKEUP (screencap returns black when screen off)
}
