#!/usr/bin/env bash
# android-mini init:用 PDF viewer(at.tomtasche.reader)打开 Kevin 简历 Kevin_CV.pdf,
# 供"读简历"类 case 起始。MobileWorld SendInterviewInvitationTask 的 start state 在
# /sdcard/Download 放了 Kevin_CV.pdf(需先 /task/init reset)。
set -euo pipefail

adb_bin="${ANDROID_ADB_BIN:-${ADB:-adb}}"
pdf="${ANDROID_MINI_INIT_PDF:-/sdcard/Download/Kevin_CV.pdf}"
serial_args=()
if [[ -n "${ANDROID_SERIAL:-}" ]]; then
  serial_args=(-s "$ANDROID_SERIAL")
fi

# content:// (SAF externalstorage) 比 file:// 可靠:at.tomtasche.reader 接 file:// 会渲染白屏
# (读不到 PDF 内容);content:// 才渲染(20260627_141353 step2 回归)。先 content://,失败回退 file://。
"$adb_bin" "${serial_args[@]}" shell am start -a android.intent.action.VIEW \
  -d "content://com.android.externalstorage.documents/document/primary%3ADownload%2FKevin_CV.pdf" \
  -t application/pdf >/dev/null 2>&1 \
  || "$adb_bin" "${serial_args[@]}" shell am start -a android.intent.action.VIEW \
    -d "file://${pdf}" -t application/pdf >/dev/null 2>&1

sleep "${ANDROID_MINI_INIT_SETTLE_S:-5}"
