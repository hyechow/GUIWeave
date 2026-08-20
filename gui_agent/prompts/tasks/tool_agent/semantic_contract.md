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
version: 2
---
Normalize only the predicates that decide conditional task branches.

Return `conditional_predicates` as affirmative, externally verifiable propositions whose
truth selects a branch or suppresses a fallback. Return an empty list when the task has no
conditional branch. Do not include a fallback action or other task steps in a predicate.

For a predicate evaluated from a record, state what its content affirms is already true in
the world, not that a topically named record exists. Preserve state, polarity, and time. A
record saying the state is required, pending, conditional, or will happen later does not
establish it. Convert nominalized state labels into clauses (`confirmation` → `has been
confirmed`, `approval` → `has been approved`, `cancellation` → `has been cancelled`). Do not
invent a predicate or alter user-owned values. Write each record predicate as `a source
affirmatively states that <the required world state already holds>`; never state that a record,
message, or email exists.
