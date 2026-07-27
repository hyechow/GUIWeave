---
id: task.orchestrator.coding_review
source_type: task_template
platform: shared
scope:
  - orchestrator
owner: gui_agent.core.orchestrator.planner
schema: code_review
eval_suites:
version: 41
---
Review the candidate Python program against the user's task, supplied knowledge, static diagnostics,
and mock execution. The fixture is visible test data, not a canonical answer; never hardcode its
record IDs, values, or row count.

The public API is:

- `ctx.reach(goal, success={...}, target=None) -> UIState`: reach one typed non-durable UI
  postcondition and return its opaque Runtime-issued state handle. Assign and consume it.
  When its state feeds `query`, success must name the same entity; each query validates its own
  requested fields.
  Use the same collection contract before `read`.
- `ctx.query(state, entity=..., fields=[...] or {"Field": "type"}, filters={},
  coverage="complete")`: filter and materialize one collection in the current context.
  `filters` are literal source predicates. One call executes only those declared predicates.
- `ctx.read(state, target=None, fields=[...] or {"Field": "type"})`: read fields from one target
  or verified state. Field mappings may declare `text`, `number`, `money`, `datetime`, or `boolean`.
  A row returned by `ctx.query` is already a concrete target; pass the row directly. Never demand
  an invented ID, URL, action field, or separate identity lookup for that read.
- `ctx.commit(goal, target=None, values={...})`: commit one durable business operation.
- `ctx.command(capability, **arguments)`: deterministic platform capability.

Review in this order:

1. Preserve every user-requested entity, qualifier, operation, numeric relationship, and output.
2. Fix every static diagnostic. Do not trade one contract violation for another.
3. Check each `query` and `read` field against its exact available fields. Every later
   `row["field"]` or `row.get("field")` must have been projected by that call.
4. Check effect boundaries. `gui` only establishes non-durable application context; `query` owns
   local search, exact source filters, pagination, and materialization; `commit` owns durable
   business changes. Every `query`/`read` must consume a state from one preceding
   context-establishing `gui` call; when the state is already visible, that call is a mechanical
   assertion rather than forced navigation. Its `goal` must be local; assign its returned state
   and pass that exact variable to `query` or `read`. For query, literal success must name the
   same entity; do not guess document titles, page
   suffixes, sidebars, or menus.
5. Check data flow. Runtime values used for selection or relative updates must participate in the
   calculation and reach the final `commit` values. Pass an already acquired record as `target`
   instead of inventing a database identifier.
6. Check collection logic. Apply predicates before cardinality assertions. Do not select an
   arbitrary first match. For top/bottom N, require at least N qualifying records, sort by the
   authoritative runtime field, and take exactly N. Do not reinterpret a count as a time window.
   When a shorter search key is available, entity resolution must visibly query the full phrase
   first and issue a second literal query with the shorter phrase only when the first result is
   empty. Reject hidden or unconditional broadening, generated fuzzy scoring, and invented
   matching modes.
7. Resolve the mock runtime error with the smallest causal change. Do not add prerequisite writes,
   speculative validation, unrelated navigation, or redundant display parsers.

`query(filters=...)` is the only planning-level representation of source-native filtering. Do not
add a separate GUI task to apply a filter. Currency and semantically numeric table values are
declared as numeric types; typed date/time values are Python `datetime` objects.

`ctx.reach` returns a UI state capability, not business data; assign it only to feed `query`/`read`.
`ctx.commit` returns no business value. Never put filters in either call. Put changed business
fields in `commit(values=...)`. Each typed Statement
owns only its in-scope widget mechanics and proof: `query` acquisition owns pagination, while
`commit` owns save/commit behavior.

Treat task qualifiers as selection predicates unless the user explicitly asks to create or change
them. Preserve valid runtime-derived filtering and calculations. Delete unused values and redundant
reads. Every newly added assertion needs a short nonempty message.

Return exactly one JSON object and no explanation or Markdown. Report issues only; never edit or
rewrite the program.

If correct:

{"approve": true, "issues": []}

Otherwise return concise structured issues:

{"approve": false, "issues": [{"code": "SHORT_STABLE_CODE",
"message": "Specific task-semantic or contract problem."}]}
