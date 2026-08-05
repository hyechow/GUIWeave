---
id: knowledge.android.Messages.navigation
source_type: knowledge_navigation
platform: android
app: Messages
scope:
  - decompose
  - orchestrator
  - planner
  - replanner
source: mobileworld_app_contract
confidence: medium
sensitivity: internal
ttl: session
version: 1
---
# Messages on Android

## Interface contract

- `Messages` collection:
  - query fields: `id`
  - source filter fields: `body` (literal substring)
  - ordering: newest first
- `Message` detail; not a collection query interface:
  - identity fields: `id`
  - detail fields: `body`
  - mutation fields: `reply`
- A `Messages` result does not carry the message text used by another application;
  cross-application use of that text requires the concrete `Message.body` detail.
- A reply applies to one concrete message identity and preserves the requested reply
  text exactly.
