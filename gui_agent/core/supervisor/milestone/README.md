# Milestone Execution Boundary

This package executes one interactive milestone at a time. It is application- and
platform-neutral. Site facts belong in knowledge; platform mechanics belong in adapters.

## Modules

- `policy.py`: owns milestone state transitions. It may consume typed evidence, request one
  proposal, advance, recover, or fail. It must not classify instruction prose or implement a
  platform control mechanism.
- `action_protocol.py`: interprets structured planner and executor receipts (`atomic_role`,
  `action_family`, dispatch, target verification). It contains no action-verb vocabulary.
- `evidence.py`: converts observations and receipts into `EvidenceClaim` values. It never changes
  milestone state.
- `execution_scope.py`: isolates history by observable resource identity or milestone identity.
  It does not know application routes or entity names.
- `helpers.py`: assembles model calls and implements structural target-state/acquire helpers over
  the neutral `Observation` contract. Target matching uses declared `target_controls` and
  `target_values`, not text extracted from milestone prose.
- `runtime.py`: small timing and collection-loop utilities.
- `decomposition.py`: translates a goal into milestone contracts.
- `stuck.py`: recovery support based on progress evidence; it does not own completion.

## Admission Rules

Core execution code may depend on:

1. Program and milestone contracts.
2. Adapter-normalized observations.
3. Structured planner metadata and executor receipts.
4. Generic progress and completion evidence.

Core execution code must not contain:

1. Benchmark, site, page, product, or task identifiers.
2. Regex or stopword classifiers over planner/checker prose that alter control flow.
3. Browser-, Android-, or iPhone-specific control operations.
4. A second path that can mark a milestone done outside `policy.py`.

When a live failure exposes a missing concept, add a neutral contract or adapter evidence channel.
If the behavior cannot be stated without application vocabulary, keep it in knowledge or remove it.
