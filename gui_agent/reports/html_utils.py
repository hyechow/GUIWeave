"""Shared HTML helpers for report renderers."""

from __future__ import annotations

def _safe(text: str | None) -> str:
    if not text:
        return ""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
