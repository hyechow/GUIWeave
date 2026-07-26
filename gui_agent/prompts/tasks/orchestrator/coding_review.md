---
id: task.orchestrator.coding_review
source_type: task_template
platform: shared
scope:
  - orchestrator
owner: gui_agent.core.coding_orchestrator.planner
schema: code_review
eval_suites:
version: 34
---
Review the candidate Python program against the user's task, supplied knowledge, static diagnostics,
and mock execution. The fixture is visible test data, not a canonical answer; never hardcode its
record IDs, values, or row count.

The public API is:

- `ctx.gui(goal, success={...}, target=None) -> UIState`: reach one typed non-durable UI
  postcondition and return its opaque Runtime-issued state handle. Assign and consume it.
  When its state feeds `query`, success must be
  `{"entity": <same entity>, "fields": <all query fields>}`.
  Use the same collection contract before `read`.
- `ctx.query(state, entity=..., fields=[...], filters={}, field="name", fallback=None,
  coverage="complete")`: filter and materialize one collection in the current context.
- `ctx.read(state, target=None, fields=[...])`: read fields from one target or verified state.
- `ctx.write(task, target=None, values={...})`: perform one durable business operation.
- `ctx.command(capability, **arguments)`: deterministic platform capability.

Review in this order:

1. Preserve every user-requested entity, qualifier, operation, numeric relationship, and output.
2. Fix every static diagnostic. Do not trade one contract violation for another.
3. Check each `query` and `read` field against its exact available fields. Every later
   `row["field"]` or `row.get("field")` must have been projected by that call.
4. Check effect boundaries. `gui` only establishes non-durable application context; `query` owns
   local search, exact source filters, pagination, and materialization; `write` owns durable
   business changes. Every `query`/`read` must consume a state from one preceding
   context-establishing `gui` call; when the state is already visible, that call is a mechanical
   assertion rather than forced navigation. Its `goal` must be local; assign its returned state
   and pass that exact variable to `query` or `read`. For query, literal success must copy that
   query's entity and fields into `success`; do not guess document titles, page
   suffixes, sidebars, or menus.
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

`ctx.gui` returns a UI state capability, not business data; assign it only to feed `query`/`read`.
`ctx.write` returns no business value. Never put filters in either call. Put changed business
fields in `write(values=...)`. Each typed Statement
owns only its in-scope widget mechanics and proof: `query` acquisition owns pagination, while
`write` owns save/commit behavior.

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
