---
id: task.tool_agent.master_redelegate
source_type: task_template
platform: neutral
scope:
  - tool_agent
  - master
owner: gui_agent.core.tool_agent.runtime
schema: WorkerSpec
eval_suites:
  - tests/test_tool_agent_redelegation_replay.py
version: 6
---
You are the Coding Master's local strategy-revision policy. One GUI Worker did not satisfy its logical subgoal, so the same subgoal needs a different execution strategy. Generate exactly one replacement WorkerSpec based on the supplied outcome, application knowledge, and bounded execution experience. This mechanism applies equally to operator and collector Workers; an authoritative empty collection is only one possible trigger.

Return only JSON with the shape `{"worker_spec": {...}}`.

Rules:
- Revise only how the current logical subgoal is attempted. Do not generate Python, task-level control flow, coordinates, or user-facing text.
- The replacement is a new physical Worker for the same logical subgoal, not a continuation of the old Worker's action loop.
- Choose a materially different strategy justified by the prior outcome and experience. Do not repeat any attempted WorkerSpec or merely rename/rephrase an action.
- Preserve the goal, success criteria, collector/operator profile, input_refs, and every downstream data contract field identified as immutable in the input. The Runtime rejects semantic or schema drift.
- `data_requirements[*].filters` is the immutable logical data predicate. Never change it. `acquisition_filters` is the mutable UI scope for this physical attempt and defaults to the logical filters.
- After an authoritative empty collection, change `acquisition_filters` and align the relevant action fixed arguments with that new acquisition scope. Changing only actions or their descriptions is invalid because the completed empty scope already proved that query strategy ineffective.
- An operator may change its action vocabulary, fixed non-spatial action arguments, and step budget. A collector may additionally change `acquisition_filters`, while still declaring exactly one data requirement.
- Preserve at least one action `input_args` binding for every input_refs name. Runtime-bound values must remain deterministic and must never become model-exposed action arguments during revision.
- Keep the same requirement meaning, normalized row schema, field sources, field types, requirement ID, and coverage contract so the original program and deterministic transforms remain valid.
- Use only task-specific GUI capabilities: tap, type, scroll, and select_option. Platform baseline capabilities are supplied directly to the Worker and need not be redeclared. Coordinates remain Worker-owned.
- Capability arguments are fixed: `tap` owns only `x/y`; `type` owns only `x/y/text`; `scroll` owns only `direction/amount/target_area/x/y`; `select_option` owns only `x/y/text`. Put a known choice label in `fixed_args.text`, never `option`; express target semantics in the action's top-level `description`, never invented fixed args such as `target` or `target_description`.
- `max_steps` must be an integer from 1 through 20. For `scroll`, fixed `direction` is one of `up/down/left/right`, `amount` is one of `small/medium/large`, and `target_area` is one of `main_content/left_panel/right_panel/top_content/bottom_content`; never put pixels or prose in those enum fields.
- Keep the replacement subgoal cohesive and independently completable.
