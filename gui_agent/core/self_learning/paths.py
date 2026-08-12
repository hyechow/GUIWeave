"""Filesystem locations for built-in and user-installed application knowledge."""

from __future__ import annotations

import os
import sys
from pathlib import Path


BUILTIN_KNOWLEDGE_ROOT = Path(__file__).resolve().parents[3] / "knowledge"


def get_user_knowledge_root() -> Path:
    configured = os.environ.get("GUIWEAVE_KNOWLEDGE_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    if sys.platform == "darwin":
        return (
            Path.home() / "Library" / "Application Support" / "GUIWeave" / "knowledge"
        ).resolve()
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return (data_home / "guiweave" / "knowledge").expanduser().resolve()


def knowledge_roots() -> tuple[Path, ...]:
    """Return roots in precedence order, with user-installed knowledge first."""

    user = get_user_knowledge_root()
    builtin = BUILTIN_KNOWLEDGE_ROOT.resolve()
    return (user,) if user == builtin else (user, builtin)


__all__ = ["BUILTIN_KNOWLEDGE_ROOT", "get_user_knowledge_root", "knowledge_roots"]
