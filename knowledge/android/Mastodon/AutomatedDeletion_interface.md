---
id: knowledge.android.Mastodon.automated_deletion
source_type: knowledge_interface
platform: android
app: Mastodon
scope:
  - orchestrator
selector_when: automatically delete old posts automated deletion age threshold pinned favs favorites reblogs boosts
source: mastodon_web_contract
confidence: high
ttl: session
---
# Mastodon automated post deletion

- This account policy exists only in authenticated Mastodon Web settings; the native
  Android `Behavior` page cannot read or change it. Preserve the task's required
  account identity when establishing the independent Web session.
- `Automated post deletion` is a top-level settings destination. Its one form contains
  enabled state, age threshold, six independent boolean exceptions (`keep_direct`,
  `keep_pinned`, `keep_self_fav`, `keep_self_bookmark`, `keep_polls`, `keep_media`),
  favorite and boost minimums, and one `Save changes` commit.
- On this form, “only pinned posts” is an exact exception set: `keep_pinned=true`
  and every other boolean exception listed above is `false`.
