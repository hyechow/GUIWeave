---
id: knowledge.android.Mastodon.data_export
source_type: knowledge_interface
platform: android
app: Mastodon
scope:
  - orchestrator
selector_when: export follows following CSV settings import download
source: mobileworld_official_trajectory
confidence: high
ttl: session
---
# Mastodon data export

- The Android client has no account data export control. Use an existing authenticated
  Mastodon web tab in Chrome, open the web settings gear, then navigate through
  `Import and export` and download the `Follows` CSV.
- The follows export downloads as `following_accounts.csv`. If the requested output
  name differs, finish the operation by renaming the downloaded file in Files.
