---
id: knowledge.android.Mastodon.data_import
source_type: knowledge_interface
platform: android
app: Mastodon
scope:
  - orchestrator
selector_when: import muted muting accounts CSV file Downloads upload merge overwrite
source: mastodon_web_contract
confidence: high
ttl: session
---
# Mastodon account-list import

- Account-list CSV import exists only in authenticated Mastodon Web settings, under
  `Import and export` → `Import`; native `Privacy and reach` and `Filters` are not
  import surfaces.
- The import contract is two-stage: select the exact import type, file, and mode, then
  `Upload`; verify the filename/type/count review and activate `Confirm`. Upload alone
  is not the mutation commit.
- After confirmation, the matching `Recent imports` row must reach `Finished` with its
  full imported count. A notice that the upload “will be processed” is not terminal.
  The table is static, so reload the Chrome page while it is processing; do not tap
  the non-interactive row as a refresh action.
