---
id: context.router.shared_goal
source_type: context_block
platform: shared
scope:
  - router
owner: gui_agent.core.chat.session
schema: RouterResult
eval_suites:
  - evals/android/router
  - evals/browser/router
  - evals/iphone/router
version: 4
---
Apply these semantic normalization rules whenever generating `goal`:

- Use the language of the current user instruction. Preserve user-provided
  literals in their original language and spelling; do not translate search terms,
  values to enter, quoted reply text, names, labels, dates, or times.
- Preserve every explicitly named application or site, search term, input value,
  time range, condition, and output requirement. If a time range has already been
  resolved to concrete dates, preserve its date format and both endpoints exactly.
- Preserve relative expressions such as "today" or "the last month". Never expand
  them using the Router host's current date, and never convert a calendar period or
  calendar-month interval into a fixed number of days.
- When the original instruction is terse and the relationship among a time, object,
  property, or metric is ambiguous, clarify only that semantic relationship.
- When a time range qualifies an extreme or ranking metric, attach the user's actual
  time expression directly to the metric being read. Preserve the range granularity:
  a single day remains a single day and a multi-day range keeps both endpoints.
- Do not add facts, units, formats, constraints, or explanatory exclusions that the
  user did not provide.
- Do not rewrite one definite target as optional implementation alternatives or add
  an "A or B" branch absent from the instruction.
- Do not assume UI structure, controls, pages, navigation paths, or operational
  steps. The runtime determines them from the actual interface.
