# Supervisor Turn Replay

The replay runner accepts current EventJournal v2 run directories and restores StatementRuntime
from the prior turn snapshot before asking the production checker/planner for the next decision.
It does not contain a legacy context adapter.

The historical directories under this folder are retained only as structured observation assets
for focused deterministic unit tests. Their old `context.json` files are not valid replay inputs.
A production adapter must not migrate or ignore those old turn shapes. Deterministic Journal v2
state-replay goldens live under `tests/fixtures/runtime_replay/` and run in the normal pytest suite.
A current replay directory contains:

- `context.json`: real `PolicyTurn` history, with report-only token/timing/prompt snapshots removed.
- `screenshot_turn_N.png`: the frame seen by the supervisor.
- `observation_turn_N.json`: structured adapter evidence for that frame.
- `replay_expectation.json`: the expected decision contract.

The screenshot is excluded from version control by the repo-wide `replay/**/*.png`
ignore rule, so a freshly checked-out fixture is missing it; drop a `screenshot_turn_N.png`
in place (or regenerate one from a live run) before replaying — `load_observation_snapshot`
refuses to load an observation whose adjacent PNG is absent.

Replay a current run with:

```bash
uv run python -m replay \
  logs/gui_agent/webarena/browser/<run-id> --turn <N>
```

`--turn` always means the Journal Turn shown by the runtime and report. Replay follows that
event's persisted `observation_url`; screenshot filenames are internal assets and may have a
different suffix in a derived resume run.

The command exits nonzero when the live supervisor or optional action-policy decision violates the
expectation. It never dispatches the returned action. A screenshot without its observation JSON is
intentionally rejected because it cannot reproduce DOM controls, filters, traversal state, or
semantic target evidence.

The pytest goldens cover Journal-to-ProgramRuntime replay and a full process-boundary loop resume.
This command covers a different seam: re-running one checker/planner decision against a saved
observation. A newly promoted decision fixture must use EventJournal schema v2; do not add a legacy
context adapter to make the historical assets executable.
