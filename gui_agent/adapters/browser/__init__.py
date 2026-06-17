"""Browser adapter: screenshot-first web-page control via Playwright + Chrome (CDP).

Implements gui_agent.core.runtime.contracts against a desktop Chrome attached over the
Chrome DevTools Protocol (CDP, default http://localhost:9222). The device drives
the page through a desktop pointer (mouse click / wheel / drag) and the keyboard,
takes PNG screenshots, and exposes browser-only extras (navigate / go_back).

The action policy still reasons over the raw screenshot. Browser perception may
also attach read-only structural metadata (URL/title/form state/table snapshots)
so checkers and read steps do not have to infer invisible browser chrome or
structured tables from pixels alone. Coordinates are normalized 0-1000 over the
browser viewport and denormalized to viewport pixels by the executor.

Import-light by design — submodules pull in ``playwright`` only on demand, so
importing this package itself stays cheap and keeps ``core.runtime.factory`` adapter-free.
"""
