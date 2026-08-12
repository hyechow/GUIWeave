# Android evals

- `loading/`: screenshot and semantic-tree loading-state cases.
- `target_verify/`: marked-frame target verification cases.

These evals are opt-in because some cases call the configured vision model:

```bash
uv run python evals/android/loading/test_loading.py
uv run python evals/android/target_verify/test_target_verify.py
```
