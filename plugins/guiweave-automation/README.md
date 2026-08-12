# GUIWeave Automation plugin

This directory defines the repo-marketplace plugin for the GUIWeave macOS Developer
Preview. It combines one Codex Skill with one local stdio MCP server. Runtime code
remains in the repository root, while platform executables are included under
`assets/` so an installed plugin does not depend on a developer machine's `vendor/`.

The current source release still resolves Python runtime code from a complete GUIWeave
checkout. Unlike earlier previews, its device helper binaries are self-contained in
the plugin directory and survive installation into the Codex plugin cache.

## Install from a clone

From the repository root:

```bash
git submodule update --init --recursive webarena-verified
uv sync
uv run playwright install chromium
cp .env.example .env
codex plugin marketplace add .
codex plugin add guiweave-automation@guiweave-dev
```

Set `AGENT_CONFIG` plus the matching model gateway `BASE_URL` and `API_KEY` in `.env`
before restarting Codex. The default example uses `config.standard.yaml` with
`STANDARD_BASE_URL` and `STANDARD_API_KEY`; screenshot-bearing model slots must support
image input. Restart Codex after installing, updating, or changing model configuration.

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

Before starting the MCP server, the launcher injects absolute paths to the executable
assets shipped in the installed plugin:

- `assets/iphone-arm64/sck_server` and `mirror_daemon`;
- scrcpy 4.0's standalone `adb`, `scrcpy`, and `scrcpy-server` under
  `assets/android-arm64/`.

The bundled `adb` is the Android transport. `scrcpy` remains optional for the mirror
window and action overlay. Asset versions and SHA-256 values are recorded in
`assets/ASSET_MANIFEST.md`; third-party license notices are included alongside them.

It can also preview PDF, Markdown, and text application manuals as private knowledge.
Generated files stay inactive until the user reviews and confirms the draft in a later
turn.

The task surface is uniform across Browser, Android, and iPhone. iPhone requires an
Apple Silicon Mac (M-series), macOS 26, and the macOS iPhone Mirroring app. The
bundled `sck_server` is the only screenshot source and `mirror_daemon` is the only
input backend. It does not use WDA, XCUITest, pymobiledevice, or usbmux. Browser and
Android retain the broader macOS 13+ host target; bundled scrcpy visualization is
arm64-only, while its bundled adb transport is universal. Intel Macs use a compatible
`scrcpy` from `PATH` when visualization is requested.

The `check_environment` MCP tool validates model configuration and the selected local
platform. Browser requires a reachable Chrome CDP endpoint unless headless; Android
requires a reachable `adb` device (`scrcpy` is optional); iPhone requires an M-series
Mac, executable bundled helpers that macOS can run, and a visible Mirroring window.
Quarantined helpers rejected by Gatekeeper are a hard preflight failure. No API key
value is returned by the check.

## Before publishing

Run the repository tests and both validators:

```bash
(cd plugins/guiweave-automation/assets && shasum -a 256 -c SHA256SUMS)
uv run pytest
uv run python ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  plugins/guiweave-automation/skills/guiweave-local-automation
uv run python ~/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py \
  plugins/guiweave-automation
```

Do not include `.env`, login profiles, logs, screenshots, or benchmark run output in
a release archive. Do include the complete `assets/` directory. The repository's
iPhone helpers are local-preview builds; sign both with Developer ID and notarize the
downloadable release before external distribution.
