# Context Assembly

`gui_agent.prompts` loads static Markdown assets. This package assembles the dynamic
context that Tool Agent Master and visual Workers receive around those prompts.

Context is represented as small `ContextBlock` values with source metadata:

- `source_type`: `runtime_state`, `knowledge_base`, `file_reference`, etc.
- `source`: concrete producer, such as `worker_journal` or `platform_adapter`
- `ttl`: expected lifetime, such as `turn`, `session`, or `task`
- `priority`: an ordering hint for token-budget work

Keep long-lived model instructions in Markdown prompt assets. Use context blocks for
current goals, page identity, recent actions, guard feedback, app knowledge, file
references, form controls, and typed data requirements.

Core context must remain platform- and scenario-neutral. Optional DOM or accessibility
facts are assistance, never a requirement for the visual Worker path.
