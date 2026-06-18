---
id: docs.prompts.readme
source_type: docs
platform: shared
scope:
  - docs
owner: gui_agent.prompts
version: 1
---

# Prompt Assets

Long model-facing prompts live here as Markdown files with frontmatter metadata.
Python code should keep the runtime mechanics: schema classes, guards,
post-processing, observation/history assembly, and conflict resolution.

Each prompt or context file declares at least:

- `id`: stable lookup id used by `load_prompt_text(...)`
- `source_type`: `task_template`, `context_block`, or `docs`
- `platform`: `iphone`, `browser`, `android`, or `shared`
- `scope`: where the text can be injected
- `owner`: module responsible for loading it
- `eval_suites`: focused tests/evals to run when editing the prompt

Use Markdown body text for the model-visible instructions. Keep app- or
site-specific facts in `knowledge/<platform>/<app>/`, not in shared prompt
assets.
