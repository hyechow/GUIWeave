# GUIWeave Automation plugin

This directory defines the repo-marketplace plugin for the GUIWeave macOS Developer
Preview. It combines one Codex Skill with one local stdio MCP server. Runtime code
remains in the repository root so the plugin does not duplicate GUIWeave or its
dependencies.

This is intentionally not a standalone plugin archive. Install it from a complete
GUIWeave repository clone; copying only this directory will not include the runtime.

## Install from a clone

From the repository root:

```bash
git submodule update --init --recursive webarena-verified
uv sync
uv run playwright install chromium
codex plugin marketplace add .
codex plugin add guiweave-automation@guiweave-dev
```

Restart Codex after installing or updating the plugin.

The marketplace declaration is `.agents/plugins/marketplace.json`. The plugin
manifest and bundled MCP server map are in `.codex-plugin/plugin.json`, and the Skill
is under `skills/guiweave-local-automation/`.

## How it launches

Codex starts `scripts/run-mcp` from this plugin directory. The launcher resolves the
repository from the configured local marketplace (or `GUIWEAVE_REPO_ROOT`), validates
that the full checkout is present, and then runs:

```bash
uv run --project <repository-root> guiweave-mcp
```

The server uses stdio only. Runtime output is redirected into each run directory so
it cannot corrupt the MCP protocol stream.

It can also preview PDF, Markdown, and text application manuals as private knowledge.
Generated files stay inactive until the user reviews and confirms the draft in a later
turn.

## Before publishing

Run the repository tests and both validators:

```bash
uv run pytest
uv run python ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  plugins/guiweave-automation/skills/guiweave-local-automation
uv run python ~/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py \
  plugins/guiweave-automation
```

Do not include `.env`, login profiles, logs, screenshots, or benchmark run output in
a release archive.
