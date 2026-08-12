# GUIWeave

GUIWeave is a local GUI automation runtime built around **Tool Agent Master**. This
repository is the macOS Developer Preview: it packages a Codex Skill plus a local
stdio MCP server, while keeping the WebArena and MobileWorld evaluation harnesses.

The preview supports:

- Chrome on macOS, through Playwright or an existing Chrome CDP session;
- Android devices and emulators, through ADB;
- per-run logs, event traces, screenshots, action visualization, HTML reports, and
  deterministic replay;
- focused unit tests and evals for Tool Agent, WebArena, and MobileWorld.

This release intentionally contains one runtime path. The previous reviewed-Python,
policy, router, and supervisor agent loops are not part of this distribution.

## Architecture

```text
Codex Skill
    └─ local stdio MCP (`guiweave-mcp`)
         └─ ToolAgentService
              └─ Tool Agent Master / visual Workers
                   ├─ Browser adapter → Chrome / Playwright
                   └─ Android adapter → ADB

Each run → context + trace + screenshots + replay + HTML report
```

The MCP process stays local. It does not expose a network listener; it launches from
the plugin and communicates with Codex over stdin/stdout.

## Requirements

- macOS 13 or later (Developer Preview target)
- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/)
- an OpenAI-compatible model configured in `.env` or the shell
- Chrome for headed browser tasks, or Playwright Chromium for headless tasks
- Android Platform Tools for Android tasks; `scrcpy` is optional for the mirror and
  action overlay

Install the runtime:

```bash
uv sync
uv run playwright install chromium
```

Copy `.env.example` to `.env` if present, or configure the provider variables used by
your local model setup. Never commit provider keys.

## Codex plugin

The distributable plugin lives in `plugins/guiweave-automation/`. From this repository:

```bash
codex plugin marketplace add .
codex plugin add guiweave-automation@guiweave-dev
```

Restart Codex after installation. The plugin contributes the
`$guiweave-local-automation` Skill and these local MCP tools:

- `check_environment`
- `run_browser_task`
- `run_android_task`
- `get_run_result`

The Skill asks Codex to preflight the platform, preserve the user's exact task, and
confirm consequential actions before execution. See
[`plugins/guiweave-automation/README.md`](plugins/guiweave-automation/README.md) for
installation and packaging details.

## Local CLI

Check a platform before the first task:

```bash
uv run guiweave check browser
uv run guiweave check android --adb-serial emulator-5554
```

For a headed Chrome session, launch the dedicated CDP profile first:

```bash
bin/launch_chrome_cdp
uv run guiweave run browser "Open the account page and report the visible plan"
```

Headless browser and Android examples:

```bash
uv run guiweave run browser "Open example.com" --headless
uv run guiweave run android "Open Settings and show the Wi-Fi page" \
  --adb-serial emulator-5554
```

Tasks operate the current signed-in UI. Use a disposable profile or test account when
possible, and explicitly review goals that can send, purchase, publish, delete, or
change account settings.

## Artifacts and replay

General runs are stored under:

```text
logs/gui_agent/tool_agent/<platform>/<timestamp>/
```

A run normally contains `context.json`, `tool_agent_trace.json`,
`tool_agent_replay.json`, screenshots, stdout/stderr logs, and `report.html`. Set
`GUIWEAVE_LOG_ROOT` to use another root.

Replay a recorded run without a device, browser, network, or model call:

```bash
bin/replay_run logs/gui_agent/tool_agent/browser/<timestamp>
```

Open or regenerate a report with:

```bash
bin/report logs/gui_agent/tool_agent/browser/<timestamp>
```

## WebArena and MobileWorld

The benchmark harnesses use the same Tool Agent runtime:

```bash
bin/webarena 11
bin/webarena --headless 11

bin/mobileworld --list
bin/mobileworld OpenFlightModeTask
```

WebArena assets and output remain under `webarena-verified/`. MobileWorld reference
assets remain under `benchmark/mobileworld/`. Benchmark-specific facts stay in their
knowledge or harness directories and are not embedded into core prompts.

## Development

```bash
uv run pytest
uv run pytest tests/test_tool_agent_runtime.py
uv run pytest evals/browser/webarena_response/test_response_replay.py
```

Core code is in `gui_agent/core/`, adapters in `gui_agent/adapters/`, reports in
`gui_agent/reports/`, knowledge in `knowledge/`, deterministic tests in `tests/`, and
evaluation cases in `evals/`.

## Preview limitations

- macOS is the tested host; Linux and Windows packaging are not yet supported.
- Browser and Android availability depends on local Chrome/CDP or ADB state.
- The plugin is source-distributed and uses `uv` to run the local MCP server.
- GUI automation is probabilistic. Use reports and replay artifacts to inspect failures.

## License

See [LICENSE](LICENSE).
