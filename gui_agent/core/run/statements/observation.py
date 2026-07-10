"""Observation state shared by consecutive immediate statements."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gui_agent.core.schemas import Observation


@dataclass
class ObservationCursor:
    """Own the latest frame while an immediate-statement batch is being drained."""

    bundle: Any
    platform: Any
    log_dir: Path
    observation: Observation | None = None
    observation_url: str | None = None

    def ensure(self, statement_index: int) -> Observation:
        if self.observation is None:
            self.refresh(f"screenshot_read_{statement_index}.png")
        assert self.observation is not None
        return self.observation

    def refresh(self, filename: str) -> Observation:
        self.observation_url = filename
        self.observation = self.bundle.make_perception(
            self.platform,
            self.log_dir / filename,
        ).observe()
        return self.observation

    @property
    def tables(self):
        return getattr(self.observation, "tables", None) if self.observation is not None else None
