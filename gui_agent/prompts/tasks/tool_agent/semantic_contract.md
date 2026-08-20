---
id: task.tool_agent.semantic_contract
source_type: task_template
platform: shared
scope:
  - tool_agent
  - master
owner: gui_agent.core.tool_agent.orchestrator
schema: TaskSemanticContract
eval_suites:
  - tests/test_tool_agent_orchestrator.py
version: 5
---
Normalize only task semantics whose type would otherwise be lost during orchestration.

Return `conditional_predicates` as affirmative, externally verifiable propositions whose
truth selects a branch or suppresses a fallback. Return an empty list when the task has no
conditional branch. Do not include a fallback action or other task steps in a predicate.

The following rule applies only to `conditional_predicates`. For a conditional predicate
evaluated from a record, state what its content affirms is already true in
the world, not that a topically named record exists. Preserve state, polarity, and time. A
record saying the state is required, pending, conditional, or will happen later does not
establish it. Convert nominalized state labels into clauses (`confirmation` → `has been
confirmed`, `approval` → `has been approved`, `cancellation` → `has been cancelled`). Do not
invent a predicate or alter user-owned values. Write each record predicate as `a source
affirmatively states that <the required world state already holds>`; never state that a record,
message, or email exists.

Return `semantic_predicates` as externally verifiable conditions that determine whether a
source entity is in scope but require whole-source Evidence because the task names no single
field that contains them. Preserve the user's semantic wording; do not rewrite an ordinary
selection predicate as `a source affirmatively states that ...` or require a textual assertion
that the user did not require. Exclude navigation, actions, output grain, and field-local name,
date, number, order, or range comparisons. Return an empty list when all selection predicates
are safely attributable to declared source fields.

Return `counted_entity` as the exact user-language noun phrase whose cardinality the requested
single-number answer represents. Preserve the target entity rather than its source container or
parent record. Return an empty string when the task does not request a count.
