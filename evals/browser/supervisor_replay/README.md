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
  observations. It is a negative provenance case: incomplete evidence must not authorize a guessed
  `Save`.

Dispatch-response replay:

- `105939_turn12`: a direct `commit + activate` Save on one editor surface redirected to a list
  with a success response. The milestone must finish as `accepted_unverified`; reopening the row
  to visually re-check the saved value is a regression. Its source metadata documents the
  migration from the old runtime's downgraded receipt to the raw planner role stored in the same
  production context.
- `112455_persistence_flow`: one complete live child/parent transaction. Use
  `expectation_turn_29.json`, `expectation_turn_30.json`, and `expectation_turn_31.json` with
  `--expectation` to verify child generation, the resource Save, and URL-response completion.
- `091305_nested_commit`: a child workflow action was emitted as `commit`, then returned to the
  resource editor without crossing a persistence boundary. The recorded turn 34 must keep the
  statement pending and dispatch the outer resource's `Save`; child-surface planner metadata must
  not consume the statement's terminal commit requirement.
- `111415_terminal_frontier`: the same child/parent boundary with a harder outer frame: generated
  rows are present but still draft-like, while the recorded checker incorrectly reuses scroll
  history from before the child dialog and proposes reopening it. Structured mutation progress
  must override that non-authoritative route diagnosis and request the root-surface commit.
- `151422_pending_save`: a root Save was dispatched, but the next frame still shows an in-flight
  loading state and no persistence response. The dispatch must remain pending; it is neither a
  completed mutation nor permission to submit the same action again.

Filter-intent binding replay:

- `140905_turn26`: the latest write receipt carries a semantic field identity while the current
  adapter observation exposes one concrete populated control and one matching applied-filter
  entry. The state/receipt pair must resolve to that concrete route and complete the filter
  milestone without dispatching a synthetic `stop` action.
- `151422_filter_residual`: a new target filter inherits an unrelated applied filter from the
  preceding page. The runtime must remove only that residual state before applying the target;
  silently stacking both filters can turn a valid lookup into a false zero-result search.

Structured mutation-capability replay:

- `152920_choice_surface`: three unmodified browser observation snapshots from the same active
  choice surface. Turns 24/25 contain extra selected options and must resolve to `preparing`;
  turn 26 has no extras and must resolve to one authorized target write. This fixture is consumed
  by `tests/test_mutation_replay.py` and intentionally needs no screenshot because the mutation
  kernel operates only on the recorded structured observation.
- `205258_intermediate_transition`: the real configuration-wizard observation has the complete
  declared choice set on an intermediate surface. Completion blocks further target writes, but the
  next workflow transition remains `prepare`; local subject completion must not manufacture a
  terminal commit boundary.

Target-directed acquire replay:

- `170119_target_acquire`: the program declares the semantic capability
  `configurations_collection`, while the adapter exposes the visual section label
  `Configurations` below the viewport and unrelated Color/Size fields elsewhere. Turns 23-24
  show real geometry progress; turns 25-26 show a frozen target position. The acquire controller
  must keep one target identity, continue while geometry advances, and exhaust explicitly after
  repeated no-progress frames instead of drifting to the desired-value fields.
- `143530_unmet_progress`: turns 9-11 are consecutive real frames from one target-acquire
  sequence. The declared value is absent while the viewport advances toward `Add Swatch`;
  absence is normal `unmet` state and must not consume recovery retries. Use the two numbered
  expectations for the earlier frames and the default expectation for turn 11.
