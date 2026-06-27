#!/usr/bin/env bash
set -euo pipefail

mw_container="${ANDROID_MINI_MW_CONTAINER:-mobile_world_env_0}"
marker_file="${ANDROID_MINI_POST_MARKER_FILE:-/tmp/android-mini-mastodon-post-marker.txt}"

if [[ ! -s "$marker_file" ]]; then
  echo "marker file not found or empty: $marker_file" >&2
  exit 1
fi

marker="$(cat "$marker_file")"

docker exec \
  -e ANDROID_MINI_POST_MARKER="$marker" \
  "$mw_container" \
  bash -lc "cd /app/service && PYTHONPATH=/app/service/src .venv/bin/python - <<'PY'
import os
from datetime import datetime, timedelta

from mobile_world.runtime.app_helpers import mastodon

marker = os.environ['ANDROID_MINI_POST_MARKER']
expected_options = ['Joel Mokyr', 'Philippe Aghion', 'Peter Howitt']

conn, cur = mastodon.connect_to_postgres()
if conn is None or cur is None:
    raise SystemExit('failed to connect to Mastodon database')

cur.execute(
    '''
    SELECT s.id, s.text, s.created_at, p.options, p.multiple, p.expires_at
    FROM statuses s
    LEFT JOIN polls p ON p.id = s.poll_id
    WHERE s.text ILIKE %s AND s.text ILIKE %s
    ORDER BY s.id DESC
    LIMIT 1
    ''',
    ('%#vote2025%', f'%{marker}%'),
)
row = cur.fetchone()
conn.close()

if not row:
    raise SystemExit(f'no #vote2025 status found for marker {marker!r}')

status_id, text, created_at, options, multiple, expires_at = row
missing = [option for option in expected_options if option not in (options or [])]
if missing:
    raise SystemExit(f'status {status_id} missing poll options: {missing}; options={options!r}')
if multiple is not True:
    raise SystemExit(f'status {status_id} poll is not multiple choice: multiple={multiple!r}')
if not expires_at or not created_at:
    raise SystemExit(f'status {status_id} missing poll timestamps: created_at={created_at!r}, expires_at={expires_at!r}')

duration = expires_at - created_at
if not (timedelta(days=6, hours=23) <= duration <= timedelta(days=7, hours=1)):
    raise SystemExit(f'status {status_id} poll duration is not about one week: {duration}')

print(f'verified Mastodon poll status {status_id} marker={marker}')
PY"
