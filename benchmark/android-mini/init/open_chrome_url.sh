#!/usr/bin/env bash
set -euo pipefail

adb_bin="${ANDROID_ADB_BIN:-${ADB:-adb}}"
chrome_package="${ANDROID_MINI_CHROME_PACKAGE:-com.android.chrome}"
url="${ANDROID_MINI_INIT_URL:?ANDROID_MINI_INIT_URL is required}"
settle_s="${ANDROID_MINI_INIT_SETTLE_S:-6}"
keep_foreground="${ANDROID_MINI_INIT_KEEP_FOREGROUND:-0}"
proxy="${ANDROID_MINI_HTTP_PROXY:-${MW_ANDROID_HTTP_PROXY:-10.0.2.2:38888}}"
proxy_exclusions="${ANDROID_MINI_HTTP_PROXY_EXCLUDE:-10.0.2.2,localhost,127.0.0.1}"

serial_args=()
if [[ -n "${ANDROID_SERIAL:-}" ]]; then
  serial_args=(-s "$ANDROID_SERIAL")
fi

adb_cmd() {
  "$adb_bin" "${serial_args[@]}" "$@"
}

configure_proxy() {
  local raw="$proxy"
  raw="${raw#http://}"
  raw="${raw#https://}"
  raw="${raw%%/*}"
  case "$(printf '%s' "$raw" | tr '[:upper:]' '[:lower:]')" in
    ""|0|false|none|off)
      adb_cmd shell settings put global http_proxy :0 >/dev/null 2>&1 || true
      adb_cmd shell settings delete global global_http_proxy_host >/dev/null 2>&1 || true
      adb_cmd shell settings delete global global_http_proxy_port >/dev/null 2>&1 || true
      ;;
    *)
      local host="${raw%:*}"
      local port="${raw##*:}"
      if [[ -n "$host" && "$host" != "$port" && "$port" =~ ^[0-9]+$ ]]; then
        adb_cmd shell settings put global http_proxy "$host:$port" >/dev/null 2>&1 || true
        adb_cmd shell settings put global global_http_proxy_host "$host" >/dev/null 2>&1 || true
        adb_cmd shell settings put global global_http_proxy_port "$port" >/dev/null 2>&1 || true
      fi
      ;;
  esac
  adb_cmd shell settings put global global_http_proxy_exclusion_list "$proxy_exclusions" >/dev/null 2>&1 || true
}

configure_proxy
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
