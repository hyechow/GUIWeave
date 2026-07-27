# Interactive Statement Executor

This package executes one `Interact` invocation at a time. It is an agentic React executor, not a
business phase machine.

## One decision core

For each non-loading observation:

1. `StatementMemory` projects this invocation's Journal facts.
2. The screenshot supplies the portable visual baseline; adapter affordances are optional evidence.
3. Transition receives the immutable contract, Memory, current observation and relevant knowledge.
4. Transition assesses the current state and proposes one `act | complete | failed` result.
5. Runtime validates proposal shape, declared values, observable fields and target capability.
6. The adapter grounds the physical action and classifies its effect. Runtime authorizes that
   grounded effect against the typed interaction intent immediately before dispatch.
7. A valid terminal proposal becomes `StatementOutcome`.

Transition is therefore the only component that answers:

- What state is this Statement currently in?
- Where should the next operation happen, and what should it do?

Assessment is diagnostic output, not persisted state. The next frame is reconstructed from the
contract, Journal facts and current observation.

## Typed postconditions and grounded-effect authorization

Runtime does **not** hold final business authority. It may only:

| Kind | Role | On failure |
|------|------|------------|
| **Typed success predicate** | Compare a declared postcondition with adapter-normalized state | Admit or short-circuit `complete` only on a proven match |
| **Grounded permission guard** | Compare grounding-bound `effect_kind` with collection phase | Reject known forbidden effects before dispatch; unknown effects abstain |
| **Proposal mechanics** | Validate declared values, target refs, capabilities and observable-field rules | Give one same-frame correction, then fail the current Statement |
| **Hard structural** | Invented journal citations, hard-budget final frame | May terminal-fail |

`exhausted` is reserved for truly unrecoverable cases (e.g. hard-budget final frame, corrupt
transition payload after its structural correction), not for an unproven filter state or a
grounded-effect veto.

`ctx.query` lowers to three typed phases:

- **`locate_collection`**: uniquely address the structural collection.
- **`constrain_collection`**: establish an exact `FilterPredicateSet`. Missing adapter coverage,
  missing predicates and extra predicates are not success.
- **acquire**: materialize rows from the resolved collection handle.

`ctx.reach` lowers to **`reach_collection`** using the downstream entity and required fields, then
hands control back to Program as soon as that typed state exists. It may navigate or authenticate,
but cannot filter, paginate, collect, or mutate.

The restricted interaction phases admit only grounded local-view effects:
`query_control`, `presentation`, and `viewport`. Pagination, context navigation, business field
writes and business commits are outside their capability. Authorization never inspects visible
labels such as “Apply”, “Save”, or localized equivalents.

An action identifies:

- **where**: a visually readable target and optional current-frame `target_ref`;
- **what**: an action family, optional contract value and expected next-frame result.

Free-text instruction is a human-readable rendering, not a source of permission. Platform
adapters own structural effect classification; core owns the platform-neutral capability check.

## State boundary

The Journal is the only persisted fact stream. Memory is a bounded read-only projection, and
`StatementOutcome` is the only terminal value consumed by ProgramRuntime. Statement-private caches,
progress and grounding data end with the invocation.

Core prompts remain platform- and scenario-neutral. Browser, Android and iPhone mechanics live in
adapters; application facts live in knowledge. Missing DOM or accessibility data is never treated as
proof that a visually present target does not exist.
