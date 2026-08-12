# GUIWeave development notes

GUIWeave Developer Preview has one execution architecture: Tool Agent Master plans
and dispatches visual Workers through a platform-neutral `PlatformBundle`. Browser
and Android adapters own sessions, perception, action validation, execution, and
optional visualization.

## Commands

```bash
uv sync
uv run pytest
uv run guiweave --help

bin/launch_chrome_cdp
uv run guiweave run browser "open the account page"

uv run guiweave run android "open Settings" --adb-serial emulator-5554
bin/scrcpy

bin/webarena 11
bin/mobileworld --list
```

## Boundaries

- Core prompts are platform- and scenario-neutral.
- Browser, Android, WebArena, and MobileWorld mechanics stay in their adapters or
  harnesses.
- Site- and app-specific facts live under `knowledge/`, not in core prompts.
- Every run must retain its trace, screenshots, replay data, and report inputs.
- Add deterministic tests for runtime and adapter invariants; keep model-driven cases
  under `evals/`.
