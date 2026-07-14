# Milestone Execution Boundary

This package executes one interactive milestone at a time. It is application- and
platform-neutral. Site facts belong in knowledge; platform mechanics belong in adapters.

## Modules

- `policy.py`: owns milestone state transitions. It may consume typed evidence, request one
  proposal, advance, recover, or fail. It must not classify instruction prose or implement a
  platform control mechanism.
- `../../run/action_exec.py`: grounds, authorizes, and dispatches one concrete primitive. The
  concrete lifecycle role and semantic action key are fixed here, never reconstructed by history.
- `../../run/action_signals.py`: is the only runtime writer of persisted action delivery, target,
  and response facts. Sensors submit raw facts; this module cannot infer effects or transitions.
- `../../run/turns.py`: records the supplied supervisor decision and action facts without
  reclassifying them.
- `evidence.py`: converts observations and receipts into `EvidenceClaim` values. It never changes
  milestone state or reads transient monitor state. Statement scope and evidence subject/resource
  are separate dimensions.
- `../../run/execution_signals.py`: `ExecutionCoordinator` is the sole reducer of action, effect,
  and persistence claims into a completion recommendation. `policy.py` alone applies that
  recommendation and may escalate an ordinary `act` to recovery from typed progress evidence.
- `../../run/persistence.py`: projects write/commit receipts into `clean/pending/submitted`; it
  does not judge the requested business state.
- `../../run/progress_monitor.py`: emits advancing/stalled/exhausted trajectory evidence. It
  cannot call recovery or reinterpret an unmet target as rejection.
- `execution_scope.py`: isolates history by observable resource identity or milestone identity.
  It does not know application routes or entity names.
- `observation_state.py`: interprets normalized filter and target-unit state without planning or
  state transitions.
- `acquisition.py`: derives deterministic section, affordance, and target-write proposals from the
  neutral `Observation` contract.
- `model_io.py`: assembles checker/planner/selector model calls; it owns no execution transition.
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

## Target Binding

Write/select dispatch uses the platform-neutral binding protocol in
`gui_agent/core/run/target_binding.py`. A concrete visual point is the baseline capability and has
one-shot scope. Adapters may upgrade it to a structural identity or provide a direct identity
contradiction. A missing optional structural capability may fall back to visual binding. Positive
structural ambiguity never does. A unique concrete point may derive its structural unit directly;
an explicit unit is required only when the point itself cannot distinguish multiple candidates.

Binding authorizes one write dispatch only. It does not mark a milestone complete, choose a
recovery route, or maintain a second execution ledger. Completion remains owned by `policy.py`
and must be supported by post-action evidence associated with the same binding token.

## Mutation Progress

Mutation history is projected rather than stored in a second mutable transaction object. Surface
identity is fallback evidence: the first observed surface is an entry hint and an in-place commit
on another surface is non-terminal. A URL response alone never manufactures a persistence
boundary. Workflows whose only final submit occurs on a child surface must declare immediate
persistence instead of relying on a navigation heuristic. Platforms without surface identity rely
on structured roles, receipts, and completion evidence.

A nested in-place commit leaves persistence clean without a write receipt and pending after a
write; it never establishes terminal persistence by itself. Before the child flow returns to the
entry surface, forward preparation remains legal. Once that return makes persistence
`terminal_ready`, only a root commit may be dispatched; repeated non-commit proposals are dropped.
There is deliberately no second text-derived "closed preparation" state machine.
