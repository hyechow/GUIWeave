---
id: knowledge.android.Clock.navigation
source_type: knowledge_navigation
platform: android
app: Clock
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
# Clock on Android

## Interface contract

Direct alarm setting; one alarm carries all requested values in a single durable change.
No prior navigation or inspection is required.

- `time`: the alarm time as displayed text (for example `8:25 AM`).
- `ringtone`: the ringtone name (for example `beebeep`).
- `vibration`: boolean; `false` turns vibration off.
- `days`: the weekdays on which the alarm repeats, as a list of English day names
  (for example `["Saturday", "Sunday"]`).
