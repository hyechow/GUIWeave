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

# Bundled standalone adb (downloaded into vendor/ — no brew). The adbutils wheel
# ships NO adb binary, so adbutils is pointed at this via ADBUTILS_ADB_PATH (only
# when that env var is unset AND the bundled binary exists; otherwise PATH wins).
VENDORED_ADB = _REPO_ROOT / "vendor" / "scrcpy-macos-aarch64-v4.0" / "adb"

# Target device serial: "host:port" for wireless adb (e.g. 192.168.31.240:5555)
# or a USB serial. None -> auto-select the sole connected device.
DEFAULT_SERIAL = os.environ.get("ANDROID_SERIAL") or None

# Swipe pixels per unit of the neutral scroll ``amount`` (amount=5 -> ~1000px,
# a bit under half of a 2400px-tall panel). First-pass value; calibrate later.
SCROLL_PX_PER_AMOUNT = 200

# Downscale the captured screenshot to this WIDTH (px), preserving aspect, before it
# becomes the Observation — cuts LLM image tokens. Coordinates are UNAFFECTED: the
# executor denormalizes 0-1000 against the DEVICE resolution (window_size), not the
# image size. 0 disables. Override via env ANDROID_SCREENSHOT_WIDTH.
SCREENSHOT_MAX_WIDTH = int(os.environ.get("ANDROID_SCREENSHOT_WIDTH") or 320)

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
