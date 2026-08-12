# GUIWeave Automation plugin

This directory is the source distribution for the GUIWeave macOS Developer Preview.
It combines one Codex Skill with one local stdio MCP server. Runtime code remains in
the repository root so the plugin does not duplicate GUIWeave or its dependencies.

## Install from a clone

From the repository root:

```bash
uv sync
uv run playwright install chromium
codex plugin marketplace add .
codex plugin add guiweave-automation@guiweave-dev
```

Restart Codex after installing or updating the plugin.

The marketplace declaration is `.agents/plugins/marketplace.json`. The plugin
manifest is `.codex-plugin/plugin.json`, the local MCP configuration is `.mcp.json`,
and the Skill is under `skills/guiweave-local-automation/`.

## How it launches

Codex starts the server from this plugin directory with:

```bash
uv run --project ../.. guiweave-mcp
```

The server uses stdio only. Runtime output is redirected into each run directory so
it cannot corrupt the MCP protocol stream.

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
