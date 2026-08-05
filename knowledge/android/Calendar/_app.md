---
id: knowledge.android.Calendar.navigation
source_type: knowledge_navigation
platform: android
app: Calendar
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
# Calendar on Android

## Interface contract

- Calendar is a separate application.
- New-entry contract:
  - existing target: none
  - preparatory entity or view: none
  - planner-visible query or mutation fields: none
  - business description: the exact source text observed in the originating application
- A generic summary or implicit active context cannot replace the business description
  because the Calendar UI resolves the source's date, time, duration, and title.
