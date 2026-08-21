"""Browser runtime profiles with explicit page-intrusion boundaries."""

from __future__ import annotations

import os
from typing import Literal, cast


BrowserRuntimeProfile = Literal["evaluation", "production"]


def resolve_browser_profile(value: str | None = None) -> BrowserRuntimeProfile:
    raw = str(value or os.environ.get("BROWSER_RUNTIME_PROFILE") or "evaluation")
    profile = raw.strip().casefold().replace("-", "_")
    if profile == "production_browser":
        profile = "production"
    if profile not in {"evaluation", "production"}:
        raise ValueError(f"unsupported browser runtime profile: {raw!r}")
    return cast(BrowserRuntimeProfile, profile)


__all__ = ["BrowserRuntimeProfile", "resolve_browser_profile"]
