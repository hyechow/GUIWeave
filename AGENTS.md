# Repository Guidelines

## Project Structure

This repository is a Python 3.11+ GUI automation agent. Core runtime, planning,
orchestration, reporting, and shared schemas live in `gui_agent/core/` and
`gui_agent/reports/`. Platform-specific code lives under `gui_agent/adapters/`
for browser, iPhone, and Android.

Knowledge files live in `knowledge/` and must contain application- or site-specific
facts only. Evaluation cases live in `evals/`; ordinary unit tests live in
`tests/`. WebArena assets, task files, and run output live under
`webarena-verified/`. Runtime logs and screenshots belong in `logs/` and should
not be treated as source unless intentionally promoted to fixtures.

Use `tmp_scripts/` for short-lived investigation scripts. Keep these scripts
focused and disposable; do not make production code depend on them.

## Prompt And Knowledge Boundaries

Code prompts must be scenario-agnostic. Do not write WebArena, Magento,
RoboTeam, iOS picker, Android alarm, browser combobox, or any other
case-specific facts into core prompts. Core prompts may contain only generalized
strategies and rules that apply across tasks, sites, and platforms.

Prefer extracting a general rule before adding knowledge. If a behavior can be
expressed as a reusable strategy, deterministic guard, validator, or platform
neutral prompt rule, implement it in code. Use knowledge files only for facts
that are inherently application-specific: page names, navigation paths, field
names, domain status meanings, business-specific acceptance semantics, or
site-specific data quirks.

Platform-specific mechanics belong in adapter prompts or adapter code, not in
core prompts. Site-specific task facts belong in that site's knowledge directory,
not in shared code.

Concrete examples:

- General rule: data-query SQL must use actual normalized column identifiers,
  not schema display text such as `Header->column`.
- General rule: aggregation tasks must use a source that contains the filter
  fields, grouping fields, and final output fields.
- Knowledge fact: a particular admin site exposes customer email in an Orders
  grid field named `customer_email`.

## Development Commands

- `uv sync`: install dependencies from `pyproject.toml` and `uv.lock`.
- `uv run pytest`: run the unit test suite.
- `uv run pytest tests/<name>.py`: run focused tests for a change.
- `uv run python evals/browser/orchestrator/test_orchestrator_decompose.py --label <text>`:
  run a focused orchestrator decomposition eval.
- `./bin/webarena <task_id>`: run a WebArena task through the local harness.

For syntax checks, prefer targeted `uv run python -m py_compile <files>`.

## Coding Style

Use idiomatic Python with 4-space indentation and type hints for public
interfaces. Prefer `snake_case` for functions, variables, modules, and scripts;
use `PascalCase` for classes. Keep async boundaries explicit.

Use `rg` for search. Keep edits scoped to the requested behavior. Do not commit
generated logs, caches, screenshots, or run output unless they are intentional
fixtures.

## Testing Guidelines

Add or update tests when changing planner, orchestrator, prompt, checker,
adapter, or report behavior. Keep tests focused on the invariant being protected.
For prompt changes, prefer eval assertions that check generalized behavior; use
site-specific eval assertions only when validating site knowledge.

Hardware- or browser-dependent tests should fail clearly when required services
or devices are unavailable. When changing WebArena behavior, run the relevant
unit tests plus the smallest matching WebArena/decomposer eval.

## Commit Style

Follow the current history style:

```text
fix(browser): plan native select option actions
feat(browser): expose form controls in observations
refactor(reports): split html report rendering
```

Use lowercase Conventional Commit subjects: `fix(scope): ...`,
`feat(scope): ...`, `refactor(scope): ...`, `docs(scope): ...`, or
`test(scope): ...`. Keep the subject imperative, concise, and focused on one
user-visible or architecture-visible change.

When the user asks to commit only specific files, stage only those files. Do not
include unrelated working-tree changes.

When grouping a larger change, split production code into coherent implementation
commits where useful. Keep all related unit tests, eval cases, fixtures, replay
scripts, and replay data together in a separate follow-up `test(...)` commit rather
than scattering test artifacts across the production commits.

## Security And Configuration

Keep provider secrets in local environment files or shell configuration, never in
source. Common variables include `API_PROVIDER`, model/provider keys, and model
names. Avoid checking in raw device or browser captures if they may contain
private data.
