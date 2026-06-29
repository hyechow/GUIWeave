#!/usr/bin/env bash
set -euo pipefail

mw_container="${ANDROID_MINI_MW_CONTAINER:-mobile_world_env_0}"
expectation="${ANDROID_MINI_MASTODON_LIST_EXPECTATION:-full}"

docker exec \
  -e ANDROID_MINI_MASTODON_LIST_EXPECTATION="$expectation" \
  "$mw_container" \
  bash -lc "cd /app/service && PYTHONPATH=/app/service/src .venv/bin/python - <<'PY'
import os

from mobile_world.runtime.app_helpers import mastodon

expectation = os.environ['ANDROID_MINI_MASTODON_LIST_EXPECTATION'].strip().lower()

if not mastodon.is_mastodon_healthy():
    raise SystemExit('Mastodon backend is not healthy')

lists = mastodon.get_lists_by_username('test') or []


def list_by_title(title: str) -> dict:
    for item in lists:
        if item.get('title') == title:
            return item
    raise SystemExit(f'list {title!r} not found; existing={[item.get(\"title\") for item in lists]!r}')


def assert_no_lists() -> None:
    if lists:
        raise SystemExit(f'expected no lists, got {[item.get(\"title\") for item in lists]!r}')


def assert_list(
    title: str,
    *,
    replies_policy: int,
    exclusive: bool,
    members: set[str] | None = None,
) -> None:
    item = list_by_title(title)
    if item.get('replies_policy') != replies_policy:
        raise SystemExit(
            f'list {title!r} replies_policy mismatch: '
            f'expected {replies_policy}, got {item.get(\"replies_policy\")!r}'
        )
    if item.get('exclusive') != exclusive:
        raise SystemExit(
            f'list {title!r} exclusive mismatch: '
            f'expected {exclusive}, got {item.get(\"exclusive\")!r}'
        )
    if members is not None:
        actual = {member.get('username') for member in item.get('members', [])}
        if actual != members:
            raise SystemExit(
                f'list {title!r} members mismatch: expected {sorted(members)!r}, got {sorted(actual)!r}'
            )


if expectation in {'empty', 'none'}:
    assert_no_lists()
elif expectation == 'open-created':
    assert_list('open', replies_policy=1, exclusive=False, members=set())
elif expectation == 'open-members':
    assert_list(
        'open',
        replies_policy=1,
        exclusive=False,
        members={'openCompany', 'openUniversity'},
    )
elif expectation == 'cute-created':
    assert_list(
        'open',
        replies_policy=1,
        exclusive=False,
        members={'openCompany', 'openUniversity'},
    )
    assert_list('cute', replies_policy=0, exclusive=True, members=set())
elif expectation == 'cute-pupper':
    assert_list(
        'open',
        replies_policy=1,
        exclusive=False,
        members={'openCompany', 'openUniversity'},
    )
    assert_list('cute', replies_policy=0, exclusive=True, members={'pupper'})
elif expectation == 'cute-pupper-kitty':
    assert_list(
        'open',
        replies_policy=1,
        exclusive=False,
        members={'openCompany', 'openUniversity'},
    )
    assert_list('cute', replies_policy=0, exclusive=True, members={'pupper', 'kitty'})
elif expectation == 'full':
    assert_list(
        'open',
        replies_policy=1,
        exclusive=False,
        members={'openCompany', 'openUniversity'},
    )
    assert_list(
        'cute',
        replies_policy=0,
        exclusive=True,
        members={'pupper', 'kitty', 'olivia'},
    )
else:
    raise SystemExit(f'unknown ANDROID_MINI_MASTODON_LIST_EXPECTATION: {expectation}')

print(f'verified Mastodon list expectation: {expectation}')
PY"
