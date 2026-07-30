# Android Statement Replay

Curated fixtures in this directory preserve real Android screenshots, adapter observation
snapshots, and the bounded Journal prefix needed to reproduce production decisions. Transition
cases use `python -m replay`; single-frame Read cases use `python -m replay.read`. Neither path
dispatches an action.

Run the Android boundary collection with:

```bash
bin/replay_suite replay/suites/android_key.json
```
