# Evals

The Developer Preview keeps focused evals for Tool Agent and its two benchmark surfaces:

- `tool_agent/` documents deterministic Master/Worker contract, replay, grounding, data-store, and report gates.
- `browser/` covers browser perception, DOM grounding, and WebArena response projection.
- `android/` covers loading and target-verification perception used by MobileWorld runs.

Ordinary deterministic regressions run with `uv run pytest`. Evals that call a configured LLM or require Playwright assets remain opt-in and document their own command.
