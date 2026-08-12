"""Canonical platform names shared by every GUIWeave client surface."""

from __future__ import annotations

from typing import Literal, get_args


PlatformName = Literal["browser", "android", "iphone"]
PLATFORMS: tuple[PlatformName, ...] = get_args(PlatformName)


__all__ = ["PLATFORMS", "PlatformName"]
