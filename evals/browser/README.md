# Browser evals

- `dom_snap/`: deterministic DOM coordinate correction cases.
- `perception/`: native form-control extraction in an isolated Playwright page.
- `webarena_response/`: deterministic promoted replays plus an opt-in LLM classification suite against WebArena-Verified task intents.

Run a focused eval directly, for example:

```bash
uv run pytest evals/browser/webarena_response/test_response_replay.py
uv run python evals/browser/perception/test_perception.py
```
