# Interactive Statement Executor

This package executes one `Interact` invocation at a time. It is an agentic React executor, not a
business phase machine.

## One decision core

For each non-loading observation:

1. `StatementMemory` projects this invocation's Journal facts.
2. The screenshot supplies the portable visual baseline; adapter affordances are optional evidence.
3. Transition receives the immutable contract, Memory, current observation and relevant knowledge.
4. Transition assesses the current state and proposes one `act | complete | infeasible` result.
5. Runtime mechanically validates the proposal. An invalid proposal is returned to Transition once
   on the same frame; Runtime never repairs it into another business action.
6. A valid action is grounded by the action policy and dispatched by the adapter. A valid terminal
   proposal becomes `StatementOutcome`.

Transition is therefore the only component that answers:

- What state is this Statement currently in?
- Where should the next operation happen, and what should it do?

Assessment is diagnostic output, not persisted state. The next frame is reconstructed from the
contract, Journal facts and current observation.

## Mechanical boundaries

Runtime may reject a proposal only when its structure is invalid, a Journal citation is invented, a
current-frame capability is contradicted, a value is outside the contract, completion evidence is
insufficient, or a hard budget is exhausted. These checks do not choose page routes, business targets
or fallback tactics.

An action identifies:

- **where**: a visually readable target and optional current-frame `target_ref`;
- **what**: an action family, optional contract value and expected next-frame result.

Free-text instruction is a human-readable rendering, not a second source of control flow.

## State boundary

The Journal is the only persisted fact stream. Memory is a bounded read-only projection, and
`StatementOutcome` is the only terminal value consumed by ProgramRuntime. Statement-private caches,
progress and grounding data end with the invocation.

Core prompts remain platform- and scenario-neutral. Browser, Android and iPhone mechanics live in
adapters; application facts live in knowledge. Missing DOM or accessibility data is never treated as
proof that a visually present target does not exist.
