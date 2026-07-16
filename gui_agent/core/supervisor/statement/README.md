# Statement Execution Boundary

This package executes one interactive statement at a time. It is application- and
platform-neutral. Site facts belong in knowledge; platform mechanics belong in adapters.

## Modules

- `policy.py`: builds Memory/evidence inputs, invokes one LLM Transition, and applies
  hard Guard vetoes. A veto triggers a bounded, stateless Statement-local replan on the same
  contract and observation; it does not imply Statement infeasibility or Program redecomposition.
  The policy must not choose a business route from deterministic phases.
- `../../run/statement_memory.py`: projects the active instance's Journal facts into bounded,
  read-only LLM context. It stores no phase and writes no parallel ledger.
- `../../run/statement_transition.py`: validates completion, infeasible, and evidence-reference
  boundaries. It never chooses a fallback action.
- `../../run/action_exec.py`: grounds and dispatches one concrete primitive. The
  concrete lifecycle role and semantic action key are fixed here, never reconstructed by history.
- `../../run/action_signals.py`: is the only runtime writer of persisted action delivery, target,
  and response facts. Sensors submit raw facts; this module cannot infer effects or transitions.
- `../../run/turns.py`: records the supplied supervisor decision and action facts without
  reclassifying them.
- `evidence.py`: converts observations and receipts into `EvidenceClaim` values. It never changes
  statement state or reads transient monitor state. Statement scope and evidence subject/resource
  are separate dimensions.
- `../../run/execution_signals.py`: `CompletionReducer` reduces action, effect, and persistence
  claims into `satisfied/pending/contradicted` terminal evidence. It has no route output.
- `../../run/persistence.py`: projects write/commit receipts into `clean/pending/submitted`; it
  does not judge the requested business state.
- `execution_scope.py`: isolates history by observable resource identity or statement identity.
  It does not know application routes or entity names.
- `observation_state.py`: interprets normalized filter and target-unit state without planning or
  state transitions.
- `model_io.py`: assembles the single Transition model call; it owns no facts or runtime state.
- `runtime.py`: small timing and collection-budget utilities.
- `action_normalization.py`: keeps picker action metadata internally consistent; it owns no flow.

## Admission Rules

Core execution code may depend on:

1. Program and statement contracts.
2. Adapter-normalized observations.
3. Structured Transition action metadata and executor receipts.
4. Generic completion and hard-budget evidence.

Every dispatched action remains a Journal-backed Memory receipt. Narrative history may be
compacted, but receipts, effects, failures, and constraints remain addressable by `turn:N`.
Visual semantic completion is explicitly `accepted_unverified`; only authoritative adapter or
Journal evidence may produce `confirmed`.

Core execution code must not contain:

1. Benchmark, site, page, product, or task identifiers.
2. Regex or stopword classifiers over Transition prose that alter control flow.
3. Browser-, Android-, or iPhone-specific control operations.
4. A second path that can mark a statement done outside `policy.py`.

When a live failure exposes a missing concept, add a neutral contract or adapter evidence channel.
If the behavior cannot be stated without application vocabulary, keep it in knowledge or remove it.

## Target Binding

Write/select dispatch uses the platform-neutral binding protocol in
`gui_agent/core/run/target_binding.py`. A concrete visual point is the baseline capability and has
one-shot scope. Adapters may upgrade it to a structural identity or provide a direct identity
contradiction. A missing optional structural capability may fall back to visual binding. Positive
structural ambiguity never does. A unique concrete point may derive its structural unit directly;
an explicit unit is required only when the point itself cannot distinguish multiple candidates.

Binding records where one write dispatch landed. It does not authorize a route, mark a statement
complete, choose recovery, or maintain a second execution ledger. Completion remains owned by
the `Transition → Guard` boundary and must be supported by post-action evidence.

## Mutation Progress

Mutation history is projected rather than stored in a second mutable transaction object. Surface
identity is fallback evidence: the first observed surface is an entry hint and an in-place commit
on another surface is non-terminal. A URL response alone never manufactures a persistence
boundary. Workflows whose only final submit occurs on a child surface must declare immediate
persistence instead of relying on a navigation heuristic. Platforms without surface identity rely
on action roles, receipts, and completion evidence.

A nested in-place commit leaves persistence clean without a write receipt and pending after a
write; it never establishes terminal persistence by itself. `terminal_ready` is exposed to the
LLM as a fact, not converted into a commit-only phase. Guard rejects writes outside the declared
contract and unsupported terminal proposals; Memory lets the LLM avoid repeating irreversible
effects without introducing a second repeat-state machine.
