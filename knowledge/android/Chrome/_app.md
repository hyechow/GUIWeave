---
id: knowledge.android.Chrome.navigation
source_type: knowledge_navigation
platform: android
app: Chrome
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
# Chrome on Android

## Interface contract

Chrome is a web browser. A web-search task produces a results view whose visible answer is
read directly from the active screen; there is no structured collection.

- The results view is established with one navigation naming the searched topic; the search
  phrase is the task's literal text and must appear unchanged in that navigation.
- The requested visible scalar (for example a temperature) is one typed value read from the
  active view; a temperature answer is returned as one integer.
