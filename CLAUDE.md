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
- Mechanisms must be built from generic signals — ARIA roles/states, semantic HTML,
  structural heuristics, behavioral probing. Vendor CSS classes and other site-family
  executable facts (e.g. `.admin__*`, `.mage-*`, `data-ui-id`) are forbidden in core and
  adapters, including as `knowledge/` selectors: an agent that depends on site facts is
  not a general agent. Descriptive vocabulary tokens (`title`, `pager`, `required`,
  `filter`, `datepicker`, `selectmenu`) and cross-site component libraries are platform
  mechanism, not site fact. Evaluations are regression nets for generality, never the
  design target.
- Every run must retain its trace, screenshots, replay data, and report inputs.
- Add deterministic tests for runtime and adapter invariants; keep model-driven cases
  under `evals/`.
