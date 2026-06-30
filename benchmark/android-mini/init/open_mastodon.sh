#!/usr/bin/env bash
# android-mini init: cold-start Mastodon and optionally seed Mastodon list DB state
# for independently debuggable MastodonManageMultiListTask mini cases.
set -euo pipefail

adb_bin="${ANDROID_ADB_BIN:-${ADB:-adb}}"
mastodon_package="${ANDROID_MINI_MASTODON_PACKAGE:-org.joinmastodon.android.mastodon}"
mw_container="${ANDROID_MINI_MW_CONTAINER:-mobile_world_env_0}"
start_backend="${ANDROID_MINI_START_MASTODON_BACKEND:-0}"
list_state="${ANDROID_MINI_MASTODON_LIST_STATE:-}"
list_entry="${ANDROID_MINI_MASTODON_LIST_ENTRY:-}"
proxy="${ANDROID_MINI_HTTP_PROXY:-${MW_ANDROID_HTTP_PROXY:-}}"
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

prepare_list_state() {
  [[ -n "$list_state" ]] || return 0

  docker exec \
    -e ANDROID_MINI_MASTODON_LIST_STATE="$list_state" \
    "$mw_container" \
    bash -lc "cd /app/service && PYTHONPATH=/app/service/src .venv/bin/python - <<'PY'
import os

from mobile_world.runtime.app_helpers import mastodon

state = os.environ['ANDROID_MINI_MASTODON_LIST_STATE'].strip().lower()

if not mastodon.is_mastodon_healthy():
    if not mastodon.start_mastodon_backend():
        raise SystemExit('failed to start Mastodon backend')
if not mastodon.is_mastodon_healthy():
    raise SystemExit('Mastodon backend is not healthy')

conn, cur = mastodon.connect_to_postgres()
if conn is None or cur is None:
    raise SystemExit('failed to connect to Mastodon database')

expected_users = [
    'test',
    'openCompany',
    'openUniversity',
    'pupper',
    'kitty',
    'olivia',
]
cur.execute(
    '''
    SELECT id, username
    FROM accounts
    WHERE username = ANY(%s)
      AND domain IS NULL
    ''',
    (expected_users,),
)
account_ids = {username: account_id for account_id, username in cur.fetchall()}
missing_accounts = sorted(set(expected_users) - set(account_ids))
if missing_accounts:
    raise SystemExit(f'missing Mastodon accounts: {missing_accounts}')

test_account_id = account_ids['test']
cur.execute(
    '''
    SELECT f.id, dst.username
    FROM follows f
    JOIN accounts dst ON dst.id = f.target_account_id
    WHERE f.account_id = %s
    ''',
    (test_account_id,),
)
follow_ids = {username: follow_id for follow_id, username in cur.fetchall()}


def clear_test_lists() -> None:
    cur.execute('SELECT id FROM lists WHERE account_id = %s', (test_account_id,))
    list_ids = [row[0] for row in cur.fetchall()]
    if list_ids:
        cur.execute('DELETE FROM list_accounts WHERE list_id = ANY(%s)', (list_ids,))
        cur.execute('DELETE FROM lists WHERE id = ANY(%s)', (list_ids,))


def create_list(title: str, replies_policy: int, exclusive: bool, members: list[str]) -> None:
    cur.execute(
        '''
        INSERT INTO lists (account_id, title, created_at, updated_at, replies_policy, exclusive)
        VALUES (%s, %s, NOW(), NOW(), %s, %s)
        RETURNING id
        ''',
        (test_account_id, title, replies_policy, exclusive),
    )
    list_id = cur.fetchone()[0]
    for username in members:
        follow_id = follow_ids.get(username)
        if follow_id is None:
            raise SystemExit(f'missing follow from test to {username}')
        cur.execute(
            '''
            INSERT INTO list_accounts (list_id, account_id, follow_id, follow_request_id)
            VALUES (%s, %s, %s, NULL)
            ''',
            (list_id, account_ids[username], follow_id),
        )


clear_test_lists()

if state in {'empty', 'none'}:
    pass
elif state == 'dirty':
    create_list('old-open', 2, False, ['openCompany'])
    create_list('old-cute', 1, True, ['pupper'])
elif state == 'open-created':
    create_list('open', 1, False, [])
elif state == 'open-members':
    create_list('open', 1, False, ['openCompany', 'openUniversity'])
elif state == 'cute-created':
    create_list('open', 1, False, ['openCompany', 'openUniversity'])
    create_list('cute', 0, True, [])
elif state == 'cute-pupper':
    create_list('open', 1, False, ['openCompany', 'openUniversity'])
    create_list('cute', 0, True, ['pupper'])
elif state == 'cute-pupper-kitty':
    create_list('open', 1, False, ['openCompany', 'openUniversity'])
    create_list('cute', 0, True, ['pupper', 'kitty'])
elif state == 'full':
    create_list('open', 1, False, ['openCompany', 'openUniversity'])
    create_list('cute', 0, True, ['pupper', 'kitty', 'olivia'])
else:
    raise SystemExit(f'unknown ANDROID_MINI_MASTODON_LIST_STATE: {state}')

conn.commit()
cur.close()
conn.close()
print(f'prepared Mastodon list state: {state}')
PY"
}

tap_text() {
  local label="$1"
  local bounds
  local xml_file
  adb_cmd shell uiautomator dump /sdcard/window.xml >/dev/null
  xml_file="$(mktemp)"
  adb_cmd shell cat /sdcard/window.xml >"$xml_file"
  bounds="$(
    ANDROID_MINI_TAP_LABEL="$label" python3 - "$xml_file" <<'PY'
import os
import re
import sys

label = os.environ["ANDROID_MINI_TAP_LABEL"]
xml = open(sys.argv[1], encoding="utf-8").read()
for node in re.finditer(r'<node [^>]*>', xml):
    text = re.search(r'\btext="([^"]*)"', node.group(0))
    desc = re.search(r'\bcontent-desc="([^"]*)"', node.group(0))
    value = (text.group(1) if text else '') or (desc.group(1) if desc else '')
    if value != label:
        continue
    bounds = re.search(r'\bbounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', node.group(0))
    if not bounds:
        continue
    x1, y1, x2, y2 = map(int, bounds.groups())
    print((x1 + x2) // 2, (y1 + y2) // 2)
    break
PY
  )"
  rm -f "$xml_file"
  if [[ -z "$bounds" ]]; then
    echo "text not found in current Android UI: $label" >&2
    return 1
  fi
  adb_cmd shell input tap $bounds
}

open_list_entry() {
  case "$(printf '%s' "$list_entry" | tr '[:upper:]' '[:lower:]')" in
    ""|0|false|none|off)
      return 0
      ;;
    menu|lists|manage|add-cute|cute-add)
      if [[ "$(printf '%s' "$list_entry" | tr '[:upper:]' '[:lower:]')" == "menu" ]]; then
        tap_text "Home" || adb_cmd shell input tap 190 205
        sleep 0.8
        return 0
      fi
      ;;
    *)
      echo "unknown ANDROID_MINI_MASTODON_LIST_ENTRY: $list_entry" >&2
      return 1
      ;;
  esac

  if ! tap_text "Lists"; then
    tap_text "Home" || adb_cmd shell input tap 190 205
    sleep 0.8
    tap_text "Lists"
  fi
  sleep 0.8

  case "$(printf '%s' "$list_entry" | tr '[:upper:]' '[:lower:]')" in
    lists)
      return 0
      ;;
  esac

  tap_text "Manage lists"
  sleep 1.5

  case "$(printf '%s' "$list_entry" | tr '[:upper:]' '[:lower:]')" in
    manage)
      return 0
      ;;
  esac

  tap_text "cute"
  sleep 1
  tap_text "List members"
  sleep 1
  tap_text "Add member"
  sleep 1.5
}

case "$(printf '%s' "$start_backend" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes|on)
    docker exec "$mw_container" bash -lc "cd /app/service && PYTHONPATH=/app/service/src .venv/bin/python - <<'PY'
from mobile_world.runtime.app_helpers import mastodon
if not mastodon.is_mastodon_healthy():
    if not mastodon.start_mastodon_backend():
        raise SystemExit('failed to start Mastodon backend')
if not mastodon.is_mastodon_healthy():
    raise SystemExit('Mastodon backend is not healthy')
print('mastodon backend ready')
PY"
    ;;
esac

configure_proxy
prepare_list_state

adb_cmd shell am force-stop "$mastodon_package" >/dev/null 2>&1 || true
adb_cmd shell monkey -p "$mastodon_package" -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1

sleep "${ANDROID_MINI_INIT_SETTLE_S:-5}"
open_list_entry
