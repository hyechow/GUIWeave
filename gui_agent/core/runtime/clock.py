"""Frozen, source-aware platform clock snapshots for deterministic tasks."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict

from gui_agent.core.runtime.platforms import PlatformName


class PlatformTimeSnapshot(BaseModel):
    """One task reference time together with its provenance and confidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    platform: PlatformName
    local_datetime: str
    timezone: str
    utc_offset: str
    source: Literal["browser_cdp", "android_adb", "host_fallback"]
    confidence: Literal["authoritative", "fallback"]
    captured_at: str
    fallback_reason: str = ""

    def relative_date_offsets(self) -> dict[str, str]:
        """Map nearby calendar-day offsets to dates using this frozen clock."""

        anchor = datetime.fromisoformat(self.local_datetime).date()
        return {
            str(offset): (anchor + timedelta(days=offset)).isoformat()
            for offset in range(-2, 3)
        }


def _offset_text(now: datetime) -> str:
    compact = now.strftime("%z")
    return f"{compact[:3]}:{compact[3:]}" if len(compact) == 5 else compact


def host_time_fallback(
    platform: PlatformName,
    *,
    reason: str,
) -> PlatformTimeSnapshot:
    """Capture the host clock while making the weaker provenance explicit."""

    now = datetime.now().astimezone()
    zone = getattr(now.tzinfo, "key", None) or str(now.tzinfo or "")
    return PlatformTimeSnapshot(
        platform=platform,
        local_datetime=now.isoformat(timespec="seconds"),
        timezone=zone,
        utc_offset=_offset_text(now),
        source="host_fallback",
        confidence="fallback",
        captured_at=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        fallback_reason=reason,
    )


def platform_time_from_parts(
    platform: PlatformName,
    *,
    local_datetime: str,
    timezone_name: str,
    utc_offset: str,
    source: Literal["browser_cdp", "android_adb"],
) -> PlatformTimeSnapshot:
    return PlatformTimeSnapshot(
        platform=platform,
        local_datetime=local_datetime,
        timezone=timezone_name,
        utc_offset=utc_offset,
        source=source,
        confidence="authoritative",
        captured_at=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
    )


__all__ = [
    "PlatformTimeSnapshot",
    "host_time_fallback",
    "platform_time_from_parts",
]
