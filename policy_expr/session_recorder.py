"""Session persistence: record chat CLI sessions for visualization."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def _now_local() -> datetime:
    return datetime.now()


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _atomic_write(path: Path, data: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        os.write(fd, data.encode("utf-8"))
        os.close(fd)
        os.rename(tmp, path)
    except Exception:
        os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class SessionRecorder:
    """Record one CLI session's conversation history; write to disk on exit."""

    def __init__(self, sessions_root: Path, supervisor: str = "", action_policy: str = "") -> None:
        self._started_at = _now_local()
        self._id = self._started_at.strftime("%Y%m%d_%H%M%S")
        self._session_dir = sessions_root / self._id
        self._entries: list[dict] = []
        self._turn_counter = 0
        self._supervisor = supervisor
        self._action_policy = action_policy

    @property
    def session_dir(self) -> Path:
        return self._session_dir

    def next_turn_dir(self) -> Path:
        """Create and return the next turn directory (turn_1, turn_2, ...)."""
        self._turn_counter += 1
        d = self._session_dir / f"turn_{self._turn_counter}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def add(self, entry: dict) -> None:
        """Append a conversation turn record."""
        entry["timestamp"] = _now_iso()
        self._entries.append(entry)

    def save(self) -> Path | None:
        """Write session.json. Returns path or None if empty."""
        if not self._entries:
            return None
        self._session_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "session_id": self._id,
            "started_at": self._started_at.astimezone().isoformat(),
            "ended_at": _now_iso(),
            "supervisor": self._supervisor,
            "action_policy": self._action_policy,
            "entries": self._entries,
        }
        path = self._session_dir / "session.json"
        _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))
        return path
