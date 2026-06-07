"""Browser adapter factory: the one place browser construction is wired together.

Builds a :class:`policy_expr.core.factory.PlatformBundle` whose callables
construct the browser session (Chrome over CDP), executor, perception, action
policy and supervisor. Core orchestration receives the neutral bundle and never
imports these classes directly. Mirrors ``adapters/iphone/factory.py``.

SCROLL-COLLECT NOT YET SUPPORTED
--------------------------------
The iphone-shaped scroll/stitch bundle fields back the runner's scroll-collect /
stitching branch, which the browser adapter does NOT implement yet:
  - ``apply_scroll_profile`` is the identity (no per-platform scroll profiles).
  - ``make_scroll_probe`` / ``make_stitch_accumulator`` / ``robust_shift`` /
    ``gray_u8`` raise ``NotImplementedError``.
WARNING: this branch IS reachable on ordinary browser goals — the milestone
supervisor plans completion_strategy='scroll_until_boundary' for information-
gathering goals (e.g. "read all items on this page"), so such a goal raises
mid-run. Until browser collection is built (the neutral stitch algos
robust_shift/gray_u8/StitchAccumulator can be reused; only the scroll-probe is
iphone-specific), restrict the browser platform to direct-action goals.
The status reporter (HUD) is None: browser has no on-screen agent HUD yet.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from policy_expr.core.factory import PlatformBundle

if TYPE_CHECKING:
    from policy_expr.core.contracts import ActionPolicy, SupervisorPolicy


# Registries (mirror the iphone adapter shape). Browser is vision-only with a
# single action policy today; the default supervisor is structure-neutral milestone.
_POLICY_NAMES: tuple[str, ...] = ("browser_vision",)
_SUPERVISOR_NAMES: tuple[str, ...] = ("milestone",)


def _build_action_policy(name: str) -> "ActionPolicy":
    from policy_expr.adapters.browser.policies import BrowserActionPolicy

    registry: dict[str, type] = {BrowserActionPolicy.name: BrowserActionPolicy}
    try:
        return registry[name]()
    except KeyError as exc:
        choices = ", ".join(sorted(registry))
        raise ValueError(f"未知策略 {name!r}，可选：{choices}") from exc


def _build_supervisor(name: str) -> "SupervisorPolicy":
    # Browser defaults to the structure-neutral milestone supervisor (the iphone
    # SimpleSupervisorPolicy is iphone-specific and intentionally not offered here).
    from policy_expr.core.supervisor.milestone.policy import MilestoneSupervisorPolicy

    registry: dict[str, type] = {MilestoneSupervisorPolicy.name: MilestoneSupervisorPolicy}
    try:
        return registry[name]()
    except KeyError as exc:
        choices = ", ".join(sorted(registry))
        raise ValueError(f"未知监督者 {name!r}，可选：{choices}") from exc


def _apply_scroll_profile(action: object, profile: object) -> object:
    """Identity: browser has no per-platform scroll profiles (no-op pass-through)."""
    return action


_SCROLL_COLLECT_MSG = (
    "browser scroll-collect not yet supported (the milestone supervisor planned "
    "completion_strategy='scroll_until_boundary'). Use a direct-action goal, or run "
    "such collection on the iphone platform. See the SCROLL-COLLECT note in "
    "adapters/browser/factory.py."
)


def _make_scroll_probe(session: object, executor: object, log_dir: object) -> object:
    """Raised if a scroll_until_boundary milestone reaches the runner's collection branch (browser collection not yet supported)."""
    raise NotImplementedError(_SCROLL_COLLECT_MSG)


def _make_stitch_accumulator(*args: object, **kwargs: object) -> object:
    """Raised if a scroll_until_boundary milestone reaches the runner's collection branch (browser collection not yet supported)."""
    raise NotImplementedError(_SCROLL_COLLECT_MSG)


def _robust_shift(*args: object, **kwargs: object) -> object:
    """Raised if a scroll_until_boundary milestone reaches the runner's collection branch (browser collection not yet supported)."""
    raise NotImplementedError(_SCROLL_COLLECT_MSG)


def _gray_u8(png_bytes: bytes) -> object:
    """Raised if a scroll_until_boundary milestone reaches the runner's collection branch (browser collection not yet supported)."""
    raise NotImplementedError(_SCROLL_COLLECT_MSG)


def build_browser_bundle(
    *,
    backend: Optional[str] = None,
    cdp_url: Optional[str] = None,
    start_url: Optional[str] = None,
    **_ignored: object,
) -> PlatformBundle:
    """Construct the browser PlatformBundle.

    ``backend`` is accepted for signature parity with the iphone factory (no
    browser backends today). ``cdp_url`` / ``start_url`` flow through to the
    session (default CDP http://localhost:9222, overridable via env CHROME_CDP_URL).
    """
    from policy_expr.adapters.browser.executor import BrowserExecutor
    from policy_expr.adapters.browser.perception import BrowserPerception, BrowserSession

    return PlatformBundle(
        platform="browser",
        open_session=lambda: BrowserSession(cdp_url=cdp_url, start_url=start_url),
        make_executor=lambda session: BrowserExecutor(session),
        make_perception=lambda session, png_path: BrowserPerception(session, png_path),
        make_action_policy=_build_action_policy,
        make_supervisor=_build_supervisor,
        make_status_reporter=lambda enabled: None,  # browser has no HUD yet
        make_scroll_probe=_make_scroll_probe,
        apply_scroll_profile=_apply_scroll_profile,
        make_stitch_accumulator=_make_stitch_accumulator,
        robust_shift=_robust_shift,
        gray_u8=_gray_u8,
        default_action_policy="browser_vision",
        default_supervisor="milestone",
        action_policy_choices=_POLICY_NAMES,
        supervisor_choices=_SUPERVISOR_NAMES,
    )
