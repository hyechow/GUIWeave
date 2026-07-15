"""Browser scroll-probe: a trivial vertical-progress check + cache profile.

Unlike the iphone ``ScrollProbe`` (which tries multiple gestures/locations because
mirror scrolling is unreliable and needs per-app calibration), browser wheel scroll
is DETERMINISTIC — so the probe just performs the requested scroll once, measures the
vertical shift via the neutral ``core.vision.stitch.robust_shift``, and reports success iff
the page advanced. This satisfies the runner's scroll-collect contract:

  - ``make_scroll_probe(phone, executor, log_dir)`` -> ``BrowserScrollProbe``
  - ``probe(before_png, action, *, turn_no)`` -> result(success, profile, reason);
    the probe EXECUTES the scroll (so the runner marks the turn executed on success).
  - ``apply_profile(action, profile)`` -> reuse the verified method/direction while resolving a
    fresh safe page anchor on every frame.

Boundary detection is handled by the runner: when a cached scroll yields zero shift it
discards the cache and re-probes; the re-probe then returns failure (no progress),
which ends the collection statement.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from gui_agent.core.schemas import Action
from gui_agent.core.vision.stitch import MIN_PROGRESS, _gray_u8, robust_shift

# Browser screenshots (CDP Page.captureScreenshot) are full-page content — no device
# frame, no iOS status bar — so the stitch content band is the whole frame.
_FULL_TOP, _FULL_BOT = 0.0, 1.0
_SETTLE_S = 0.5


@dataclass(frozen=True)
class BrowserScrollProfile:
    """A verified-working browser scroll. ``ticks``/``delta_px`` exist only to satisfy
    the runner's cached-path call ``execute_scroll(cached, ticks=, delta_px=)`` — the
    browser executor ignores them (wheel scroll is deterministic)."""

    method: str = "scroll"
    x: float = 500.0
    y: float = 500.0
    direction: str = "down"
    ticks: int = 0
    delta_px: int = 0
    observed_shift_px: int = 0
    confidence: float = 0.0


@dataclass(frozen=True)
class BrowserScrollProbeResult:
    success: bool
    profile: BrowserScrollProfile | None
    after_png: bytes | None
    attempts: int
    reason: str


def browser_robust_shift(prev_u8, cur_u8) -> tuple[int, float]:
    """Vertical shift on full-frame browser screenshots (no mask, whole-frame band)."""
    return robust_shift(prev_u8, cur_u8, content_top=_FULL_TOP, content_bot=_FULL_BOT, frame_mask=None)


def apply_profile(action: Action, profile: BrowserScrollProfile) -> Action:
    """Reuse scroll behavior, not a stale absolute point from a previous frame."""
    return action.model_copy(update={"x": None, "y": None, "direction": profile.direction})


class BrowserScrollProbe:
    def __init__(self, phone, executor, log_dir):
        self.phone = phone
        self.executor = executor
        self.log_dir = log_dir

    def probe(self, before_png: bytes, action: Action, *, turn_no: int) -> BrowserScrollProbeResult:
        direction = (action.direction or "down").strip().lower()
        # Browser wheel scroll is reliable — execute once, then measure.
        self.executor.execute_scroll(action)
        time.sleep(_SETTLE_S)
        after_png = self.phone.screenshot()
        shift, conf = browser_robust_shift(_gray_u8(before_png), _gray_u8(after_png))
        # down => content moves up => shift < 0; |shift| >= MIN_PROGRESS == advanced.
        if abs(shift) >= MIN_PROGRESS:
            profile = BrowserScrollProfile(
                x=float(action.x if action.x is not None else 500),
                y=float(action.y if action.y is not None else 500),
                direction=direction, observed_shift_px=shift, confidence=conf,
            )
            print(f"  [BrowserScrollProbe] 滚动有效: shift={shift:+d}px conf={conf:.2f}")
            return BrowserScrollProbeResult(True, profile, after_png, 1, "vertical progress observed")
        print(f"  [BrowserScrollProbe] 无推进(到底/无效): shift={shift:+d}px")
        return BrowserScrollProbeResult(False, None, after_png, 1, f"no vertical progress (shift={shift})")
