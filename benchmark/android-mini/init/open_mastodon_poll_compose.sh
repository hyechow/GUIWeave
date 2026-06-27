#!/usr/bin/env bash
# android-mini init: open Mastodon compose with #vote2025 already typed and the poll
# panel expanded. This skips MobileWorld reset + Google search so the mini case can
# focus on the poll form controls.
set -euo pipefail

adb_bin="${ANDROID_ADB_BIN:-${ADB:-adb}}"
mastodon_package="${ANDROID_MINI_MASTODON_PACKAGE:-org.joinmastodon.android.mastodon}"
main_activity="${ANDROID_MINI_MASTODON_MAIN_ACTIVITY:-org.joinmastodon.android.mastodon/org.joinmastodon.android.MainActivity}"
ime="${ANDROID_MINI_ADB_IME:-com.android.adbkeyboard/.AdbIME}"
settle_s="${ANDROID_MINI_INIT_SETTLE_S:-3}"
proxy="${ANDROID_MINI_HTTP_PROXY:-${MW_ANDROID_HTTP_PROXY:-}}"
proxy_exclusions="${ANDROID_MINI_HTTP_PROXY_EXCLUDE:-10.0.2.2,localhost,127.0.0.1}"
start_backend="${ANDROID_MINI_START_MASTODON_BACKEND:-0}"
mw_container="${ANDROID_MINI_MW_CONTAINER:-mobile_world_env_0}"
prefill_poll="${ANDROID_MINI_PREFILL_POLL:-0}"
poll_option_1="${ANDROID_MINI_POLL_OPTION_1:-Joel Mokyr}"
poll_option_2="${ANDROID_MINI_POLL_OPTION_2:-Philippe Aghion}"
poll_option_3="${ANDROID_MINI_POLL_OPTION_3:-Peter Howitt}"
post_text="${ANDROID_MINI_POST_TEXT:-#vote2025}"
marker_file="${ANDROID_MINI_POST_MARKER_FILE:-}"

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

type_b64() {
  local text="$1"
  local b64
  b64="$(printf '%s' "$text" | base64 | tr -d '\n')"
  adb_cmd shell am broadcast -a ADB_INPUT_B64 --es msg "$b64" >/dev/null
}

type_field() {
  local x="$1"
  local y="$2"
  local text="$3"
  adb_cmd shell input tap "$x" "$y"
  adb_cmd shell am broadcast -a ADB_CLEAR_TEXT >/dev/null 2>&1 || true
  type_b64 "$text"
  sleep 0.5
}

case "$(printf '%s' "$start_backend" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes|on)
    docker exec "$mw_container" bash -lc "cd /app/service && PYTHONPATH=/app/service/src .venv/bin/python - <<'PY'
from mobile_world.runtime.app_helpers import mastodon
if mastodon.is_mastodon_healthy():
    print('mastodon backend already healthy')
elif not mastodon.start_mastodon_backend():
    raise SystemExit('failed to start mastodon backend')
if not mastodon.is_mastodon_healthy():
    raise SystemExit('mastodon backend not healthy')
print('mastodon backend ready')
PY"
    ;;
esac

configure_proxy
adb_cmd shell ime enable "$ime" >/dev/null 2>&1 || true
adb_cmd shell ime set "$ime" >/dev/null 2>&1 || true

if [[ -n "$marker_file" ]]; then
  marker="${ANDROID_MINI_POST_MARKER:-android-mini-$(date +%s)}"
  printf '%s' "$marker" >"$marker_file"
  post_text="#vote2025 $marker"
fi

adb_cmd shell am force-stop "$mastodon_package" >/dev/null 2>&1 || true
adb_cmd shell am start -W \
  -a android.intent.action.MAIN \
  -c android.intent.category.LAUNCHER \
  -f 0x10008000 \
  -n "$main_activity" >/dev/null 2>&1
sleep "$settle_s"

# MobileWorld emulator is 1080x2400. These taps are intentionally coarse and target
# stable Mastodon controls: compose FAB, body field, and poll toolbar icon.
adb_cmd shell input tap 962 1992
sleep 1
adb_cmd shell input tap 540 600
adb_cmd shell am broadcast -a ADB_CLEAR_TEXT >/dev/null 2>&1 || true
type_b64 "$post_text"
sleep 0.5
adb_cmd shell input tap 212 2158
sleep "$settle_s"

case "$(printf '%s' "$prefill_poll" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes|on)
    type_field 540 749 "$poll_option_1"
    type_field 540 910 "$poll_option_2"
    adb_cmd shell input tap 104 1080
    sleep 0.5
    type_field 540 1073 "$poll_option_3"
    adb_cmd shell input tap 329 1265
    sleep 0.5
    adb_cmd shell input tap 540 1694
    sleep 0.5
    adb_cmd shell input tap 557 1277
    sleep 0.5
    adb_cmd shell input tap 753 1219
    sleep "$settle_s"
    ;;
esac
