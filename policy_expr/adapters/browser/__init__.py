"""Browser adapter: VISION-ONLY web-page control via Playwright + Chrome (CDP).

Implements policy_expr.core.contracts against a desktop Chrome attached over the
Chrome DevTools Protocol (CDP, default http://localhost:9222). The device drives
the page through a desktop pointer (mouse click / wheel / drag) and the keyboard,
takes PNG screenshots, and exposes browser-only extras (navigate / go_back).

VISION-ONLY by design: the action policy reasons over the raw screenshot, NOT the
DOM / a11y tree. No element_tree, no page_key — the Observation carries pixels
only, exactly like the iphone adapter. Coordinates are normalized 0-1000 over the
browser viewport and denormalized to viewport pixels by the executor.

Import-light by design — submodules pull in ``playwright`` only on demand, so
importing this package itself stays cheap and keeps ``core.factory`` adapter-free.
"""
