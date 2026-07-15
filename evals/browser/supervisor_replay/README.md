# Supervisor Turn Replay

The replay runner accepts current EventJournal v2 run directories and restores StatementRuntime
from the prior turn snapshot before asking the production checker/planner for the next decision.
It does not contain a legacy context adapter.

The historical directories under this folder are retained only as structured observation assets
for focused deterministic unit tests. Their old `context.json` files are not valid replay inputs.
A current replay directory contains:

- `context.json`: real `PolicyTurn` history, with report-only token/timing/prompt snapshots removed.
- `screenshot_turn_N.png`: the frame seen by the supervisor.
- `observation_turn_N.json`: structured adapter evidence for that frame.
- `replay_expectation.json`: the expected decision contract.

The screenshot is excluded from version control by the repo-wide `evals/**/*.png`
ignore rule, so a freshly checked-out fixture is missing it; drop a `screenshot_turn_N.png`
in place (or regenerate one from a live run) before replaying — `load_observation_snapshot`
refuses to load an observation whose adjacent PNG is absent.

Replay a current run with:

```bash
uv run python scripts/replay_supervisor_turn.py \
  logs/gui_agent/webarena/browser/<run-id> --turn <N>
```

The command exits nonzero when the live supervisor or optional action-policy decision violates the
expectation. It never dispatches the returned action. A screenshot without its observation JSON is
intentionally rejected because it cannot reproduce DOM controls, filters, traversal state, or
semantic target evidence.
