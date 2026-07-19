# Semantic Execution Architecture

> Status: implemented. This document records the design decision behind the current runtime.

The planned completion of cross-frame collection and deterministic data processing is specified in
[Runtime Data Acquisition and Processing Design](data_acquisition_and_processing_design.md).

## Decision

GUIWeave compiles a task into a small semantic Program and resolves UI and data details at runtime.
The Program is not a recorded click path and the Statement executor is not a business finite-state
machine.

```text
User goal
  -> Compiler: semantic intent + typed dataflow + explicit control flow
  -> Interpreter: cursor + environment + If/ForEach
  -> Interact | Data | Command executors
  -> StatementOutcome
  -> ProgramOutcome
```

The production IR contains only:

```text
Interact | Data | Command | If | ForEach | Finish
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
- Data executor decides **how to derive declared outputs from actual runtime data**.
- Command executor invokes **known deterministic platform capabilities**.
- Journal records **what actually happened**.

## Node contracts

### Interact

`Interact` reaches one semantic UI postcondition through a linear React trace. It may cross pages,
dialogs or application screens. It does not contain an internal branch contract.

```python
Interact(
    bind="saved",
    goal="Update the selected record with the requested values",
    success="The selected record is saved and the requested values are observable",
    inputs={"record": ValueRef(var="selection", path=["record"])},
    required_values={"sizes": ["30", "31"]},
    returns={"record_id": OutputSpec(type="text")},
)
```

Transition receives the Statement contract, its Journal memory and current observation. It outputs
one action proposal or one terminal proposal. Its action names both:

- **where**: the visible target/control, with an optional current-frame structural reference;
- **what**: the operation family, value and expected next-frame result.

The screenshot is required. DOM, accessibility, URL, tables and form controls are optional positive
evidence. Missing structural evidence is never negative proof against a visual target.

### Data

`Data` describes a semantic derivation rather than precompiled SQL or expressions.

```python
Data(
    bind="selection",
    goal="Select the records that still need the requested values",
    inputs={"rows": ValueRef(var="observed", path=["rows"])},
    returns={"records": OutputSpec(type="list[record]")},
)
```

At runtime, the executor proposes a small typed pipeline over actual inputs and observation. A
deterministic Python kernel supports filter, sort, top/nth, projection, distinct, date bucketing,
group aggregation and dense ranking, followed by a final emit. It executes no generated Python or
SQL source. One execution failure may trigger one repair; the plan is not persisted as Program
state.

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
            bind="updated",
            goal="Update the current record",
            success="The current record is saved",
            inputs={"record": ValueRef(var="record")},
            returns={"ok": OutputSpec(type="boolean")},
        )
    ],
    collect=ValueRef(var="updated", path=["ok"]),
    into="results",
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
            returns={"records": OutputSpec(type="list[record]")},
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
                            bind="updated",
                            goal="Update the current record",
                            success="The current record is saved",
                            inputs={"record": ValueRef(var="record")},
                            returns={"ok": OutputSpec(type="boolean")},
                        )
                    ],
                    collect=ValueRef(var="updated", path=["ok"]),
                    into="results",
                ),
                Finish(
                    message="Update completed",
                    outputs={"results": ValueRef(var="results")},
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

Interpreter checks the outputs against declared `OutputSpec`s before binding them. Program completion
is reduced to one `ProgramOutcome`.

Recovery is layered by meaning:

- invalid action proposal -> same Statement, same observation, one Transition retry;
- missing completion/output evidence -> same Statement retry when budget allows;
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
