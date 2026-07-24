---
id: task.orchestrator.coding_review
source_type: task_template
platform: shared
scope:
  - orchestrator
owner: gui_agent.core.coding_orchestrator.planner
schema: code_review
eval_suites:
version: 29
---
Review the candidate Python program against the user's task, supplied knowledge, static diagnostics,
and mock execution. The fixture is visible test data, not a canonical answer; never hardcode its
record IDs, values, or row count.

The public API is:

- `ctx.gui(task, target=None)`: establish non-durable GUI context; it raises on failure.
- `ctx.query(entity, fields=[...], filters={}, field="name", fallback=None,
  coverage="complete")`: filter and materialize one collection in the current context.
- `ctx.read(target=None, fields=[...])`: read fields from one target or current state.
- `ctx.write(task, target=None, values={...})`: perform one durable business operation.
- `ctx.command(capability, **arguments)`: deterministic platform capability.

Review in this order:

1. Preserve every user-requested entity, qualifier, operation, numeric relationship, and output.
2. Fix every static diagnostic. Do not trade one contract violation for another.
3. Check each `query` and `read` field against its exact available fields. Every later
   `row["field"]` or `row.get("field")` must have been projected by that call.
4. Check effect boundaries. `gui` only establishes non-durable application context; `query` owns
   local search, exact source filters, pagination, and materialization; `write` owns durable
   business changes. If a collection is absent from the current observation, add one preceding
   context-establishing `gui` call.
5. Check data flow. Runtime values used for selection or relative updates must participate in the
   calculation and reach the final `write` values. Pass an already acquired record as `target`
   instead of inventing a database identifier.
6. Check collection logic. Apply predicates before cardinality assertions. Do not select an
   arbitrary first match. For top/bottom N, require at least N qualifying records, sort by the
   authoritative runtime field, and take exactly N. Do not reinterpret a count as a time window.
7. Resolve the mock runtime error with the smallest causal change. Do not add prerequisite writes,
   speculative validation, unrelated navigation, or redundant display parsers.

`query(filters=...)` is the only planning-level representation of source-native filtering. Do not
add a separate GUI task to apply a filter. Currency and date/time table values are already
normalized by `query`.

`ctx.gui` and `ctx.write` return no business value. Do not assign or assert their results. Never put
filters in either call. Put changed business fields in `write(values=...)`. The GUI Statement owns
page mechanics, save/commit behavior, retries, and proof of completion.

Treat task qualifiers as selection predicates unless the user explicitly asks to create or change
them. Preserve valid runtime-derived filtering and calculations. Delete unused values and redundant
reads. Every newly added assertion needs a short nonempty message.

Return exactly one JSON object and no explanation or Markdown.

If correct:

{"approve": true, "edits": []}

Otherwise return one to ten exact local edits:

{"approve": false, "edits": [{"search": "exact text copied from the candidate",
"replacement": "replacement text"}]}

Each search must match the original candidate exactly once. Change the smallest causal region and
preserve unrelated code. Do not replace the complete `run` function unless its source selection or
overall data shape is fundamentally wrong. Every edit must independently reduce diagnostics,
runtime failure, or an explicit task mismatch.
