# Statement Execution Boundary

This package executes one interactive Statement at a time. The Statement executor is
agentic: one Transition model call judges the current state and chooses the next transition.
Runtime does not advance a business phase machine behind the model.

## Decision ownership

Each non-loading frame follows one path:

1. `StatementMemory` projects this invocation's Journal facts.
2. The screenshot provides the required visual observation; `observation_view.py` projects
   optional adapter-confirmed targets and operations.
3. Transition receives the contract, Memory, current observation, screenshot, and knowledge.
4. In one response it first returns `assessment`, then one decision:
   `act | complete | infeasible`.
5. Runtime mechanically validates the proposal and either dispatches it or records a visible
   terminal validation failure. It does not rewrite the action and does not call Transition a
   second time on the same frame.

Transition is therefore the only component that answers both semantic questions:

- What state is this Statement currently in?
- Where should the next operation happen, and what should it do?

`assessment` is diagnostic output, not persisted runtime state. The next frame is reconstructed
from the contract, Journal facts, and current observation.

## Mechanical boundaries

Runtime may reject, but never repair, a Transition proposal when:

- structured output is invalid;
- a cited `turn:N` does not exist in this Statement's Memory;
- a supplied current-frame `target_ref`, or an operation on a matching advertised target,
  contradicts its affordance;
- an input/select value is outside the Statement contract;
- terminal evidence does not satisfy the completion contract; or
- a hard action/traversal budget is exhausted.

These are capability, evidence, and resource checks. They must not infer a page route, select a
business target, prohibit a semantic tactic, or turn a rejected action into infeasibility.

## Modules

- `policy.py`: drives the one-call path and materializes validated actions or outcomes.
- `model_io.py`: assembles the single `TransitionFrame` and invokes structured output once.
- `schemas.py`: defines assessment, where+what action, evidence, and terminal result shapes.
- `observation_view.py`: exposes current-frame affordances without completion or route verdicts.
- `../../run/statement_memory.py`: builds bounded, read-only Memory from the Journal.
- `../../run/statement_transition.py`: validates terminal evidence and Journal references.
- `action_normalization.py`: renders structured action fields into an executor instruction.
- `../../run/action_exec.py`: grounds and dispatches one primitive action.
- `../../run/action_signals.py`: journals dispatch, target, response, and effect receipts.
- `../../run/execution_signals.py`: reduces evidence only after Transition proposes completion.

## Context contract

`TransitionFrame` contains raw decision evidence, not precomputed route advice:

- immutable Statement contract;
- durable Journal facts, recent steps, compacted history, and last action result;
- required current screenshot and selected application knowledge;
- optional current title/URL, form state, applied filters, tables, and affordances;
- explicit affordance coverage, including `unavailable` when the platform is visual-only.

An action must name both parts explicitly:

- **where**: a visually readable `target_control` and, when available, the exact current-frame
  `target_ref`; the ref owns identity even when the rendered label contains decorative glyphs;
- **what**: `action_family`, optional contract value, and expected next-frame result.

The free-text `instruction` is diagnostic only. Runtime renders the executable instruction from
the structured fields and never parses prose with route or business regexes.

## State and platform boundaries

The Journal is the only persisted fact stream. Memory is a projection, not another ledger.
Transition output is a proposal, not an authoritative observation. `StatementOutcome` remains the
only Statement terminal value consumed by `ProgramRuntime`.

Core prompts and code stay application-neutral. Site facts belong in knowledge. Browser, Android,
and iPhone mechanics belong in adapters. Visual evidence is the portable baseline; DOM,
accessibility, controls, tables, and URL metadata are optional positive evidence. Missing or partial
adapter evidence must never be interpreted as proof that a visually present target or state does
not exist. A missing live capability should be added as normalized adapter evidence, not as a
case-specific core rule.
