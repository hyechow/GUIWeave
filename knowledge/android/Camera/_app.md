---
id: knowledge.android.Camera.navigation
source_type: knowledge_navigation
platform: android
app: Camera
scope:
  - worker
source: manual_verified
confidence: high
sensitivity: internal
ttl: session
version: 1
---
# Camera on Android

- The main shutter is a write-through control: one successful activation captures one
  photo immediately and normally leaves the app on the same viewfinder without a
  confirmation dialog.
- For a goal requesting one photo, an executed shutter receipt with no visible error is
  the terminal commit. The unchanged viewfinder is normal post-commit state, not evidence
  that the shutter should be activated again.
