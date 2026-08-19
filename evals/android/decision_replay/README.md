# Android Worker decision replay

Curated suites promote only the screenshots, observations, current Worker prompt
snapshot, replay context, and semantic expectations needed by a few critical
frames. Do not commit an entire runtime log directory.

The MobileWorld checkout suite contains five groups: `cross_app_auth`,
`login_batch`, `cart_checkout`, `grounding_edge`, and `guard_repair`.

PR CI replays the normalized recorded response without a model or device:

```bash
bin/replay_decision_suite \
  evals/android/decision_replay/mobileworld_checkout \
  --recorded --group cross_app_auth
```

Scheduled evaluation re-samples the current configured Worker model:

```bash
uv run --env-file .env bin/replay_decision_suite \
  evals/android/decision_replay/mobileworld_checkout \
  --samples 3 --group cart_checkout
```

Promote selected frames from a successful run, then review and generalize the
generated `manifest.json` before committing it.

```bash
uv run python -m replay.promote_decisions \
  logs/gui_agent/mobileworld/android/<run-id> \
  evals/android/decision_replay/<suite-name> \
  --frames 14 15 16 23
```
