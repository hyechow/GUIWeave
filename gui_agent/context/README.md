# Context Assembly

`gui_agent.prompts` loads static Markdown prompt assets. This package assembles
the runtime context that surrounds those assets.

Context is represented as small `ContextBlock` values with source metadata:

- `source_type`: `runtime_state`, `knowledge_base`, `file_reference`, etc.
- `source`: concrete producer, such as `policy_history` or `platform_adapter`
- `ttl`: expected lifetime, such as `turn`, `session`, or `task`
- `priority`: optional ordering hint for future token-budget work

Keep model-visible long-lived instructions in Markdown prompt assets. Use context
blocks for dynamic execution state: current goal, page identity, recent actions,
checker/planner guard feedback, app knowledge, file references, form controls,
and structured-read requests.
