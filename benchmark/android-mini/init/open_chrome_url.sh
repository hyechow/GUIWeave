#!/usr/bin/env bash
set -euo pipefail

adb_bin="${ANDROID_ADB_BIN:-${ADB:-adb}}"
chrome_package="${ANDROID_MINI_CHROME_PACKAGE:-com.android.chrome}"
url="${ANDROID_MINI_INIT_URL:?ANDROID_MINI_INIT_URL is required}"
settle_s="${ANDROID_MINI_INIT_SETTLE_S:-6}"
keep_foreground="${ANDROID_MINI_INIT_KEEP_FOREGROUND:-0}"

serial_args=()
if [[ -n "${ANDROID_SERIAL:-}" ]]; then
  serial_args=(-s "$ANDROID_SERIAL")
fi

"$adb_bin" "${serial_args[@]}" shell am force-stop "$chrome_package"
"$adb_bin" "${serial_args[@]}" shell am start \
  -a android.intent.action.VIEW \
  -d "$url" \
  "$chrome_package" >/dev/null

sleep "$settle_s"

case "$(printf '%s' "$keep_foreground" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes|on)
    ;;
  *)
    "$adb_bin" "${serial_args[@]}" shell am force-stop "$chrome_package"
    "$adb_bin" "${serial_args[@]}" shell input keyevent HOME
    ;;
esac
