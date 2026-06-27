#!/usr/bin/env bash
# android-mini init:打开 Files app(com.google.android.documentsui),供"找文件"类 case 起始。
set -euo pipefail

adb_bin="${ANDROID_ADB_BIN:-${ADB:-adb}}"
serial_args=()
if [[ -n "${ANDROID_SERIAL:-}" ]]; then
  serial_args=(-s "$ANDROID_SERIAL")
fi

# Previous cases may leave a document viewer in the foreground. Start from a cold
# Files task so this init is idempotent across repeated benchmark runs.
"$adb_bin" "${serial_args[@]}" shell am force-stop at.tomtasche.reader >/dev/null 2>&1 || true
"$adb_bin" "${serial_args[@]}" shell am force-stop com.google.android.documentsui >/dev/null 2>&1 || true

"$adb_bin" "${serial_args[@]}" shell am start -a android.intent.action.VIEW_DOWNLOADS >/dev/null 2>&1 \
  || "$adb_bin" "${serial_args[@]}" shell am start -n com.google.android.documentsui/com.android.documentsui.files.FilesActivity >/dev/null 2>&1 \
  || "$adb_bin" "${serial_args[@]}" shell monkey -p com.google.android.documentsui -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1

sleep "${ANDROID_MINI_INIT_SETTLE_S:-4}"
