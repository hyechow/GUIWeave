---
id: task.orchestrator.coding_review
source_type: task_template
platform: shared
scope:
  - orchestrator
owner: gui_agent.core.coding_orchestrator.planner
schema: code_review
eval_suites:
version: 20
---
Review the candidate Python script against the user's task and supplied knowledge.

The mock fixture is visible unit-test data, not a canonical answer. Use it to understand available
fields, row shapes, and concrete execution behavior. Never hardcode fixture record IDs or dynamic
values into the script.

Check only whether the code:

- selects the entities required by the task;
- derives relative values from runtime reads;
- passes relevant runtime data through named Statement `inputs` and declares the requested literal
  changes through `required_values`, without unrelated transitions;
- returns the requested information;
- runs against the supplied mock.

Review in this order:

1. Preserve the user's exact requested entities, qualifiers, operations, and output.
2. Resolve every listed static diagnostic. A candidate with any static diagnostic can never be
   approved. Do not replace one invalid call with a different contract violation.
3. Audit every `acquire` and `read` field against the exact `available_fields` for that source.
   Collection fields and detail fields are separate; move a detail-only field to `read(record, ...)`.
4. Audit projected-record data flow. Every later `record["field"]` or `record.get("field")` must
   name a field requested when that record was acquired or read. Prefer retaining and passing the
   concrete record instead of extracting an identity field that was not projected.
5. Audit runtime-value data flow. A current value read for a relative update must participate in
   the exact requested calculation, and the computed result must reach `required_values`. For
   example, an arrival or increase by N means `new = current + N`, not `new = N`.
6. Resolve the runtime error without adding unrelated lookups, prerequisite mutations, speculative
   validation, or site behavior not required by the task.
7. Mentally re-check the complete rewritten program against the same static API before returning it.

Treat task qualifiers as selection predicates, not mutation authorization. A color, size, status,
category, or other value that identifies the requested target is presumed to describe existing
business state unless the user explicitly asks to create or change that resource. Delete speculative
prerequisite checks or writes for such qualifiers; do not "ensure" them through extra interactions.
When the candidate already acquired a concrete record, pass that record through a named `inputs`
entry instead of reducing it to a database ID or copying requested literal values into `inputs`.
Put every user-requested new or changed literal in `required_values`.

Prefer the shortest corrected program that satisfies the task, but preserve every valid
runtime-derived selection and calculation. When diagnostics concern a missing projected field,
make the smallest local fix: add that field to its originating acquire/read or remove the access
only when its output is not requested. Do not rewrite unrelated data flow while repairing a local
diagnostic.
The boolean returned by `interact` means Statement React already established and verified its
`success` postcondition. Assert that boolean, but do not add a following `read` solely to prove the
same saved state again.
The postcondition is idempotent. Remove pre-reads and branches whose only purpose is to check whether
an explicitly requested add, create, ensure, or update is already satisfied. Keep a read only when
the user's branch, target selection, or value calculation genuinely consumes current business data.
Any interaction that changes business data, including comments and notifications, must use
`persistence="explicit_commit"`. Use `immediate` only for non-durable navigation or presentation
state.
Every `explicit_commit` interaction must have nonempty `required_values` containing its requested
business writes, and its boolean return value must be asserted, branched on, or returned. A bare
durable interaction can silently fail and is never approvable.
Both `inputs` and `required_values` cross the Statement boundary as JSON data. Replace Python sets
with deterministic lists; never pass a set, set comprehension, or `set(...)` result.
`UNUSED_RUNTIME_VALUE` is a causal data-flow diagnostic. If the task describes a relative change,
repair it by carrying that value through the calculation and write. Deleting the read and writing
the task's delta as an absolute value is incorrect.
If the task instead requests an absolute value and the read does not affect selection, branching,
calculation, output, or Statement inputs, delete the unnecessary read assignment completely. Do not
retain an unused read merely to establish context; the concrete acquired record already supplies
Statement context.
Words such as arrived, received, restocked, added, removed, or consumed describe a delta. Never
reinterpret their quantity as an absolute stock value. Only explicit wording such as set to,
replace with, or change to authorizes an absolute replacement. If the candidate already computes a
delta from a runtime value, preserve that expression while fixing unrelated diagnostics.

`lookup` establishes a collection scope, `acquire` reads collection fields, `read` reads current
state, and `interact` invokes one real Statement contract. Its `inputs` are prior runtime data and
its `required_values` are the values the state transition must apply. UI mechanics and target
grounding remain inside Statement React; do not require database IDs merely to call `interact`.
The first argument of `lookup` is always a textual business mention, never a previously returned
`LookupScope`. Pass an existing scope directly to `acquire`, or start a distinct lookup from text.
Every interaction must state an independently verifiable `success` postcondition. `observe_fields`
may be used when the executor needs named observation fields to prove it. `success` must be a
nonempty string, never a boolean. Every field requested from `acquire` or `read` must exist under
that exact semantic name in supplied knowledge or mock data; do not invent or rename source fields.
The exact signature is
`ctx.interact(goal, *, success: str, inputs={}, required_values={}, observe_fields=[],
persistence="immediate|explicit_commit")`.

Return exactly one JSON object and no explanation, Markdown, findings, or extra keys.

If the candidate is correct:

{"approve": true, "edits": []}

Otherwise return one to ten exact local edits:

{"approve": false, "edits": [{"search": "exact text copied from the candidate",
"replacement": "replacement text"}]}

Each `search` must match the original candidate exactly once. Change the smallest causal region and
preserve all unrelated code, selections, calculations, and interactions. Never include `def run`
and its body inside `search`, and never return the complete function. A header-only edit may include
imports immediately preceding `def run(ctx):` plus that function header when removing an invalid
top-level import. Every `search` must copy text from the
original candidate, not text produced by an earlier replacement. When static diagnostics prevented
the mock from running, do not add cleanup refactors unrelated to those diagnostics or an explicit
task mismatch. Fix all listed diagnostics and the causal task error in this single local repair
response. A partial repair that leaves even one listed diagnostic, references an undefined
replacement variable, or accesses a newly required field without adding it to the originating
acquire/read projection is invalid. Every added assert must include a short nonempty diagnostic
message. Consolidate adjacent or causally dependent fixes in the same business phase into one edit;
do not emit no-op edits.

`BUSINESS_IDENTITY_FIRST_MATCH` cannot be suppressed with `# noqa` or any other comment. It applies
when `break` chooses the first record from an acquired business collection. Replace that selection
with a complete candidate collection and an explicit cardinality assertion, then select the sole
validated member. A local algorithmic loop, such as trying known date formats, may use `break`
because it does not choose a business entity.
Ranking tasks are different from arbitrary first-match selection. For "most recent", highest,
lowest, earliest, or similar requests, acquire and filter the complete candidate set, assert it is
nonempty, rank it by the requested runtime field, and select the first ranked result. Do not assert
that the pre-ranked candidate set has cardinality one.
`INVALID_DATE_CONSTRUCTION` means a literal day is outside the valid 1..31 range. Derive month
boundaries from the first day of a month plus or minus `datetime.timedelta`; never use day zero.
