"""Real-replay tier: regression-capture of recorded runs through the real stack.

Fixtures live under ``<platform>/<scenario>/`` (e.g. ``browser/<scenario>/``); the runner is
:mod:`replay.run` (invoke via ``python -m replay``). Deterministic replay fixtures stay in
``tests/fixtures/`` — this tier holds only the LLM-driven recorded-scenario replays.
"""
