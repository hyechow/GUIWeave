---
id: knowledge.android.Settings.navigation
source_type: knowledge_navigation
platform: android
app: Settings
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
# Settings on Android

## Interface contract

Direct device settings; no collection. Each requested setting is one durable change with
the declared field and its exact value.

- flight mode: a boolean device setting; field `Flight Mode`; `false` turns it off.
  The machine name `flight_mode` is also accepted. In the GUI, open **Network & internet**
  from the Settings home page and use its **Airplane mode** toggle directly; the nested
  **Internet** page does not contain that toggle.
- screen brightness: a 0-100 level setting; field `screen_brightness`; the maximum level
  is `100`. The aliases `brightness` and `brightness_level` are also accepted.
