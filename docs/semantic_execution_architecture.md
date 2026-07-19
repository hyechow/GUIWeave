# Semantic Execution Architecture

> Status: implemented. This document records the production semantic Program and executor split.
> Interact owns UI postconditions; Data is the only current-observation business-data reader.

The detailed contracts for cross-frame collection and deterministic data processing are specified in
[Runtime Data Acquisition and Processing Design](data_acquisition_and_processing_design.md).

## Decision

GUIWeave compiles a task into a small semantic Program and resolves UI and data details at runtime.
The Program is not a recorded click path and the Statement executor is not a business finite-state
machine.

```text
User goal
  -> Compiler: semantic intent + typed dataflow + explicit control flow
  -> Interpreter: cursor + environment + If/ForEach
  -> Interact | Acquire | Data | Command executors
  -> StatementOutcome
  -> ProgramOutcome
```

The production IR contains only:

```text
Interact | Acquire | Data | Command | If | ForEach | Finish
```

The active interaction surface is currently the singleton `main`.

## Why this boundary

The compiler knows the user's requested outcome and can make control flow explicit, but it cannot
reliably know which page, control, table shape or transient UI will exist later. Runtime executors
see those facts, but they must not acquire authority over the whole task.

This produces a stable split:

- Compiler decides **what semantic work and explicit branching exist**.
- Interpreter decides **which Program node runs next**.
- Transition decides **the current UI state and the next where+what operation**.
- Acquire decides **how to expose the next window of one already-bound collection**.
- Data executor decides **what data is observable, how declared outputs are derived, and whether
  declared data acceptance conditions hold**.
- Command executor invokes **known deterministic platform capabilities**.
- Journal records **what actually happened**.

## Node contracts

### Interact

`Interact` reaches one semantic UI postcondition through a linear React trace. It may cross pages,
dialogs or application screens. It does not contain an internal branch contract and does not return
business data. Reading text, numbers, records or collections from its terminal observation belongs
to a following `Data` node.

```python
Interact(
    goal="Update the selected record with the requested values",
    success="The editor reports that the selected record was saved",
    inputs={"record": ValueRef(var="selection", path=["record"])},
    required_values={"sizes": ["30", "31"]},
)
```

Transition receives the Statement contract, its Journal memory and current observation. It outputs
one action proposal or one terminal proposal. Its action names both:

- **where**: the visible target/control, with an optional current-frame structural reference;
- **what**: the operation family, value and expected next-frame result.

The screenshot is required. DOM, accessibility, URL, tables and form controls are optional positive
evidence. Missing structural evidence is never negative proof against a visual target.

Interact completion and business-data acceptance are different decisions. Transition may complete
after the UI postcondition is established, but it cannot bind terminal-frame data into Program
scope or claim that a data predicate passed. The terminal observation is handed unchanged to an
immediately following Data node; no UI action may occur between those two nodes.

### Acquire

`Acquire` materializes one collection that Interact has already scoped. It first uses a zero-LLM
adapter traversal capability and may fall back to an independent acquisition-only React policy.
The fallback can only bind a region, page, scroll, load more or wait; it cannot change filters,
open records, expose columns, navigate elsewhere or complete the Program.

The semantic draft declares source coverage, required fields and an optional UI source-readiness
goal. The Compiler, rather than the Decomposer LLM, lowers that into a preceding
`Data(mode="inspect")`, an If that may run Interact, one final inspection and the single Acquire.

```python
Acquire(
    bind="observed",
    goal="Materialize every reachable record from the scoped collection",
    source_check=ValueRef(var="source_schema", path=["available"]),
    returns={"rows": OutputSpec(type="list[record]", coverage="complete")},
)
```

Each observed window and movement receipt is appended to EventJournal. CollectionView and
AcquireMemoryView are replayable pure projections; no private paging phase is persisted.

### Data

`Data` describes a semantic read, derivation or acceptance check rather than precompiled SQL or
expressions. It is the only owner of business data read from the current observation, including
text, numbers, records, form values, URL/title facts and visually perceived values.

```python
Data(
    bind="verification",
    goal="Read the saved sizes and determine whether both requested values are present",
    required_fields=["saved size values"],
    returns={
        "satisfied": OutputSpec(type="boolean"),
        "observed_sizes": OutputSpec(type="json"),
    },
)
```

At runtime, the executor proposes a small typed pipeline over actual inputs and observation. A
deterministic Python kernel supports filter, sort, top/nth, projection, distinct, date bucketing,
group aggregation and dense ranking, followed by a final emit. It executes no generated Python or
SQL source. One execution failure may trigger one repair; the plan is not persisted as Program
state.

Before execution, a deterministic preflight derives every binding's shape (`scalar`, `record`,
`list[record]` or `table`) through the proposed operations and checks binding references plus emit
cardinality against the declared OutputSpec. Field existence stays owned by the Data kernel and the
executed output-contract check; the preflight does not duplicate either. A record-list output is
emitted with `path=[]`, while row projection remains an explicit transform. The preflight is pure
and stores no phase.

One straight-line block may contain at most one consecutive Data operation. With no Interact,
Acquire or control-flow boundary between two computations, no new external fact has entered the
Program, so filtering, projection, grouping, aggregation, sorting, ranking and final emission belong
to one Data execution. Intermediate tables remain private to that execution; only values consumed by
If, ForEach or Finish become typed Program bindings. The Compiler rejects a draft `Data -> Data`
chain instead of silently fusing two ambiguous natural-language goals.

Data validation has three semantic results:

| Result | Meaning | Runtime handling |
| --- | --- | --- |
| satisfied | The source is readable and the declared predicate holds | emit facts and `satisfied=true` |
| unsatisfied | The source is readable and the predicate does not hold | emit facts and `satisfied=false`; this is not a Data failure |
| unavailable | The required fact cannot be read from the current source/evidence | return an infeasible outcome with source evidence and kickback |

Data has no UI mutation or correction authority. A known correction is an explicit Program branch
to another Interact followed by another Data read. An unexpected unavailable source is handled by
RecoveryRouter and remaining-Program recompilation. Data must never turn missing evidence into
`unsatisfied`, and it must never click, navigate, expose a column or change a filter while validating.

Data has a read-only `inspect` mode for runtime schema availability. It returns
`available/bindings/missing_fields`; Program `If` may then run an Interact that exposes missing
fields before Acquire. Normal Data derivation still returns `unavailable` rather than fabricating
an empty or shape-compatible result; kickback recompiles the remaining Program using the same
inspect → If → Interact → Acquire boundary.

Entity retrieval uses the same split. A compile-time `lookup` macro names the Router entity and
semantic lookup field. The Compiler lowers it to exact full-mention Interact, Data match-count
read, and a Program If that uses the Router hint only after count=0. Physical controls remain an
Interact concern; exact-versus-fallback control flow does not.

### Command

`Command` invokes a deterministic adapter capability when the destination is already known.

```python
Command(
    capability="open_url",
    arg_refs={"url": ValueRef(var="selection", path=["detail_url"])},
)
```

Supported capabilities are `open_url`, `back` and `launch_app`. If the route must be discovered
from the current UI, it remains an `Interact` goal.

### If

`If` is the only business branch mechanism. It evaluates a declared condition over a bound value
and chooses a static branch. A conditional user goal must not be hidden inside Interact prose.

### ForEach

`ForEach` iterates a materialized list with a fixed body:

```python
ForEach(
    items=ValueRef(var="selection", path=["records"]),
    item="record",
    index="position",
    body=[
        Interact(
            goal="Update the current record",
            success="The current record is saved",
            inputs={"record": ValueRef(var="record")},
        )
    ],
)
```

Membership, filtering, sorting and deduplication happen in a preceding Data node. The loop does not
generate a body from sample rows or launch runtime sub-decomposition.

### Finish

`Finish` resolves explicit ValueRefs and forms the final reply/output projection. It performs no UI
or data work.

## Program example

```python
Program(
    goal="Update every selected record",
    statements=[
        Data(
            bind="selection",
            goal="Resolve the records requested by the user",
            returns={
                "records": OutputSpec(type="list[record]", fields=["record_id"]),
            },
        ),
        If(
            cond=Condition(
                ref=ValueRef(var="selection", path=["records"]),
                cmp="empty",
            ),
            then=[Finish(message="No matching records")],
            otherwise=[
                ForEach(
                    items=ValueRef(var="selection", path=["records"]),
                    item="record",
                    body=[
                        Interact(
                            goal="Update the current record",
                            success="The current record is saved",
                            inputs={"record": ValueRef(var="record")},
                        )
                    ],
                ),
                Finish(
                    message="Update completed",
                ),
            ],
        ),
    ],
)
```

## Runtime authority

### Transition is the UI decision core

Transition combines the responsibilities that separate checker/planner calls used to split:

1. assess the current Statement state;
2. choose the next single operation, including where it applies and what it should do.

`StatementMemory` is a read-only projection of the invocation's Journal events. It preserves actions,
receipts and previously established facts across page changes without creating a parallel state store.

Mechanical validation may reject malformed output, invented Journal references, impossible platform
capabilities, contract-external values and exhausted budgets. It does not infer business routes or
repair actions with case-specific rules. A rejected proposal is fed back to Transition once on the
same observation; it is not automatically converted into infeasibility or Program recompile.

### Interpreter owns control flow

The Interpreter alone mutates the Program cursor and environment. It resolves ValueRefs, validates
typed outputs, evaluates If, creates lexical ForEach frames and collects loop results. Executors see
one invocation at a time and cannot advance the Program.

### Journal owns facts

All durable execution facts append to EventJournal. `StatementRuntimeState` and Memory are invocation-
scoped views; reports, completion and persistence are reducers. No mutable milestone status, effect
latch or report cache is a second execution authority.

## Output and recovery contracts

Each executor returns one `StatementOutcome` with a terminal phase and:

```text
outputs: dict[str, JsonValue]
```

Interpreter checks output types and declared record `fields` against `OutputSpec`s before binding them. Program completion
is reduced to one `ProgramOutcome`.

Recovery is layered by meaning:

- invalid action proposal -> same Statement, same observation, one Transition retry;
- a malformed Data plan -> one bounded repair inside the same Data Statement;
- unreadable required data -> Data infeasible, followed by Program-level correction/recompile;
- structurally infeasible Statement or contract conflict -> hot recompile at Program level;
- exhausted/failed -> terminal StatementOutcome handled by ProgramRuntime.

## Deliberate exclusions

The architecture intentionally has no production support for:

- page-specific paths, controls or business phases in the Program;
- dynamic loop bodies, runtime sub-decomposition or function/call nodes;
- precompiled SQL/Python expressions as public Program nodes;
- multiple persisted SurfaceRefs in the current release;
- parallel milestone state, effect state or mutable completion flags;
- compatibility interpretation of retired Program/context schemas.

These exclusions keep the Program structural, the executors adaptive and the fact stream singular.
