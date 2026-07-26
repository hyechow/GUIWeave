"""Deterministic replay of reviewed Python against recorded ctx responses."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import CodingRunResult
from .sandbox import FixtureSpec, execute_code


@dataclass(frozen=True)
class RecordedContext:
    lookups: dict[str, list[dict[str, Any]]]
    reads: dict[str, dict[str, Any]]
    command_results: dict[str, Any]

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RecordedContext":
        return cls(
            lookups={
                str(name).strip().casefold(): [dict(row) for row in rows]
                for name, rows in dict(value.get("lookups") or {}).items()
            },
            reads={
                str(name): dict(row)
                for name, row in dict(value.get("reads") or {}).items()
            },
            command_results=dict(value.get("command_results") or {}),
        )

    def fixture(self) -> FixtureSpec:
        return FixtureSpec(
            lookups=self.lookups,
            reads=self.reads,
            command_results=self.command_results,
        )


def replay_program(source: str, recording: RecordedContext) -> CodingRunResult:
    """Rerun from the beginning; replay never resumes unfinished GUI state."""
    return execute_code(source, recording.fixture())


def load_recorded_cases(path: Path) -> dict[int, dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        int(task_id): dict(case)
        for task_id, case in dict(payload).items()
    }


__all__ = [
    "RecordedContext",
    "load_recorded_cases",
    "replay_program",
]
