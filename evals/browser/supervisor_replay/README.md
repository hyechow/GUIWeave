# Supervisor Turn Replay

These fixtures run the production checker and planner against a recorded decision context without
constructing an action executor. Each fixture contains:

- `context.json`: real `PolicyTurn` history, with report-only token/timing/prompt snapshots removed.
- `screenshot_turn_N.png`: the frame seen by the supervisor.
- `observation_turn_N.json`: structured adapter evidence for that frame.
- `replay_expectation.json`: the expected decision contract.

The screenshot is excluded from version control by the repo-wide `evals/**/*.png`
ignore rule, so a freshly checked-out fixture is missing it; drop a `screenshot_turn_N.png`
in place (or regenerate one from a live run) before replaying — `load_observation_snapshot`
refuses to load an observation whose adjacent PNG is absent.

Run a fixture with:

```bash
uv run python scripts/replay_supervisor_turn.py \
  evals/browser/supervisor_replay/090810_turn30
```

The command exits nonzero when the live supervisor or optional action-policy decision violates the
expectation. It never dispatches the returned action. A screenshot without its observation JSON is
intentionally rejected because it cannot reproduce DOM controls, filters, traversal state, or
semantic target evidence.

Frontier regression pair:

- `102742_turn29`: the child wizard is still on its value-selection surface; the expected edge is
  its concrete `Next` control and a parent commit is forbidden.
- `090810_turn30`: the child wizard has returned to the parent editor; the expected edge is the
  concrete parent `Save` control.
