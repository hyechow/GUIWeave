---
id: knowledge.android.Mastodon.server_info
source_type: knowledge_interface
platform: android
app: Mastodon
scope:
  - orchestrator
selector_when: owner settings backend server database size PostgreSQL MB post toot
source: mobileworld_app_contract
confidence: high
ttl: session
---
# Mastodon server information

- The native Mastodon account and Chrome's Mastodon Web login are independent. A
  workflow that switches owner in native and then uses the Web backend must establish
  owner identity separately in both session domains; one does not satisfy the other.
- Only the owner Web session exposes `Administration` → `Dashboard`. The dashboard's
  `Space usage` card reports database size in its `PostgreSQL` row; preserve the
  displayed MB value for any requested owner post.
