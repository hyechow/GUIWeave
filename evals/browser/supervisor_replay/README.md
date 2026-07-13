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

- `102742_turn29`: the child wizard is still on its value-selection surface with inherited
  Size/Color values selected. Both turn 27 (before selecting the targets) and turn 29 (after the
  targets were added on top) must remove one concrete extra option; advancing with `Next` would
  authorize a Cartesian-product mutation outside the declared business change. Workflow buttons
  such as `Select All` and `Remove Attribute` are not inferred as safe cleanup primitives. Use
  `expectation_turn_27.json` for the earlier frame and the default expectation for turn 29.
- `090810_turn30`: a legacy capture whose history lacks surface identities and adjacent structured
  observations. It is a negative provenance case: an unknown surface must remain incomplete and
  must not authorize a guessed parent `Save`. The positive parent-return edge is covered below.

Persistence-boundary replay:

- `105939_turn12`: a direct `commit + activate` Save on one editor surface redirected to a list
  with a success response. The milestone must finish as `accepted_unverified`; reopening the row
  to visually re-check the saved value is a regression. Its source metadata documents the
  migration from the old runtime's downgraded receipt to the raw planner role stored in the same
  production context.
- `112455_persistence_flow`: one complete live child/parent transaction. Use
  `expectation_turn_29.json`, `expectation_turn_30.json`, and `expectation_turn_31.json` with
  `--expectation` to verify child generation, concrete parent Save, and terminal response
  completion respectively.

Filter-intent binding replay:

- `140905_turn26`: the latest write receipt carries a semantic field identity while the current
  adapter observation exposes one concrete populated control and one matching applied-filter
  entry. The state/receipt pair must resolve to that concrete route and complete the filter
  milestone without dispatching a synthetic `stop` action.

Structured mutation-capability replay:

- `152920_choice_surface`: three unmodified browser observation snapshots from the same active
  choice surface. Turns 24/25 contain extra selected options and must resolve to `preparing`;
  turn 26 has no extras and must resolve to one authorized target write. This fixture is consumed
  by `tests/test_mutation_replay.py` and intentionally needs no screenshot because the mutation
  kernel operates only on the recorded structured observation.
