# Signal-source architecture

This note describes the facts supplied to the semantic Statement Transition.
It does not define a second completion engine or a page-phase machine.

## One decision packet

Every Interact turn gives Transition one packet:

- the frozen Statement contract and resolved invocation inputs;
- a bounded StatementMemory projection from EventJournal;
- the current screenshot as the baseline observation;
- optional adapter facts such as URL, title, form controls, semantic
  affordances, tables and viewport state;
- application knowledge and temporary Runtime rejection constraints.

The screenshot is always present for GUI Interact execution. Adapter facts are
optional evidence channels: they improve precision but cannot replace visual
reasoning or become a browser-only control path.

## Authority

| Question | Authority |
|---|---|
| What happened earlier? | EventJournal action receipts and observations |
| What is visible now? | current screenshot, optionally refined by fresh adapter facts |
| What should happen next? | one LLM Transition decision |
| Can the proposed action be dispatched? | mechanical capability and grounding validation |
| Can execution continue? | hard budget and Program ownership boundaries |
| What is the statement terminal result? | one StatementOutcome |

Past model assessments are diagnostics, not replayed facts. A current adapter
fact is authoritative only for the field it actually measures: a DOM value can
confirm the value, but does not prove that an occluded control is visually
reachable. A partial table snapshot is not a complete dataset.

## Mechanical action facts

`ActionSignal` records dispatch, target and response provenance. It is not a
business-effect state and cannot advance a Statement. Mutation receipts record
only that a bound write crossed the UI boundary. Transition sees these receipts
through StatementMemory on the next frame and decides their semantic meaning.

## Rejection path

An invalid Transition action is not converted into another action by a regex or
business guard. Runtime returns the precise mechanical rejection to Transition
on the same frame once. A second invalid proposal exhausts the Statement. An
Action Policy grounding rejection follows the same same-frame re-decision path.

## Invariants

- EventJournal is the only historical fact stream.
- StatementMemory is a read-only bounded projection, never a mutable phase.
- Core prompts contain no site, benchmark or platform-specific UI recipe.
- Optional DOM/AX facts remain optional across Browser, Android, and iPhone.
- Program control flow is not inferred from observation signals.
