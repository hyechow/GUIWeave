# Interactive Statement Executor

This package executes one `Interact` invocation at a time. It is an agentic React executor, not a
business phase machine.

## One decision core

For each non-loading observation:

1. `StatementMemory` projects this invocation's Journal facts.
2. The screenshot supplies the portable visual baseline; adapter affordances are optional evidence.
3. Transition receives the immutable contract, Memory, current observation and relevant knowledge.
4. Transition assesses the current state and proposes one `act | complete | failed` result.
5. Runtime validates proposal shape and exact target-ref capability.
6. The adapter grounds the physical action to the declared target before dispatch.
7. A valid `complete` proposal becomes `StatementOutcome`; model-declared blockage remains
   diagnostic and keeps the Statement open.

Transition is therefore the only component that answers:

- What state is this Statement currently in?
- Where should the next operation happen, and what should it do?

Assessment is diagnostic output, not persisted state. The next frame is reconstructed from the
contract, Journal facts and current observation.

## Context projection

`context_projection.py` owns the pure, deterministic projection used by Transition. It performs
no summarization, truncation or budget enforcement. Transition receives this decision projection,
not a dump of every adapter index. The screenshot remains the portable visual baseline. For typed
collection phases, `reach`/`locate` receive collection structure without row payloads or form
state; `constrain` additionally receives filter controls and query actions. Raw rows belong to
Acquire, while the complete control and affordance indexes remain available to Runtime validation.

Knowledge retrieval keeps route identity separate from Statement intent: route/title may select an
exact page section, while `selector_when` fallback uses contract semantics and requires multiple
matching terms. Detailed knowledge remains in its source file; unrelated manuals are not injected
into the live frame.

## Context compression

Projection classifies affordances as `current`, `contract_target`, `supporting` or `background`;
the internal classification is never sent to the model. `context_variants.py` offers a smaller
TransitionFrame that removes only background offscreen affordances. The shared
`ContextCompressor` selects this variant only when the complete prompt exceeds its ceiling. If
safe variants are insufficient, the same compressor drops whole blocks by budget tier and TTL as
its final strategy. It does not summarize knowledge, infer facts or judge completion. One
`context_compression` report audits kept, compressed and dropped blocks.

## Typed postconditions and action validation

Runtime does **not** hold final business authority. It may only:

| Kind | Role | On failure |
|------|------|------------|
| **Typed adapter predicate** | Compare contracts only with explicit typed adapter state, such as complete applied filters | Admit or short-circuit `complete` only on a proven match |
| **Proposal mechanics** | Validate proposal schema and exact target-ref capabilities | Correct on the same frame; if output remains malformed, record a retry turn and keep the current Statement |
| **Hard runtime boundary** | Hard-budget final frame | May terminal-fail |

Contract and adapter producers normalize their values into the same typed schema before
comparison. `AcceptanceMatcher` then performs exact structural comparison and returns only
`met | unmet | unknown`; it does not parse display text or create a terminal outcome. `met` may
short-circuit completion, `unmet` keeps the Statement open, and `unknown` leaves the decision to
Transition.

An invalid Transition payload is not a business verdict. After its same-frame structural
correction is exhausted, Runtime records the diagnostic and retries the same Statement on a fresh
observation. Only a hard external budget may stop that recovery; an invalid payload itself never
produces `failed` or `exhausted`. A model-declared `failed` proposal follows the same rule.

`ctx.query` lowers to three typed phases:

- **`locate_collection`**: uniquely address the structural collection.
- **`constrain_collection`**: establish an exact `FilterPredicateSet`. Missing adapter coverage,
  missing predicates and extra predicates are not success.
- **acquire**: materialize rows from the resolved collection handle.

`ctx.reach` lowers to **`reach_collection`** using the downstream entity, required fields, and
declared expected state, then hands control back to Program as soon as that typed state exists.

An action identifies:

- **where**: a visually readable target and optional current-frame `target_ref`;
- **what**: an action family, optional contract value and expected next-frame result.

Free-text instruction is a human-readable rendering, not a source of permission. Platform
adapters may expose control facts, but runtime permission never derives from labels, DOM ids, or
inferred business effects. Runtime also does not re-check a completed semantic state by matching
ordinary DOM control labels and display strings. Label/name/id mismatches may leave a target
unresolved, but cannot veto dispatch; only an exact target-ref geometry contradiction can.

## State boundary

The Journal is the only persisted fact stream. Memory is a bounded read-only projection, and
`StatementOutcome` is the only terminal value consumed by ProgramRuntime. Statement-private caches,
progress and grounding data end with the invocation.

Core prompts remain platform- and scenario-neutral. Browser, Android and iPhone mechanics live in
adapters; application facts live in knowledge. Missing DOM or accessibility data is never treated as
proof that a visually present target does not exist.
