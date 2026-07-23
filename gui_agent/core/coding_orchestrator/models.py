"""Data contracts for the standalone coding-orchestrator experiment."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CodeDiagnostic:
    code: str
    message: str
    line: int = 0
    column: int = 0

    def render(self) -> str:
        where = f"line {self.line}:{self.column}" if self.line else "source"
        return f"[{self.code}] {where}: {self.message}"


@dataclass(frozen=True)
class TraceEvent:
    op: str
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = field(default_factory=dict)
    result: Any = None


@dataclass(frozen=True)
class WriteEvent:
    goal: str
    success: str
    target_id: str | None = None
    inputs: dict[str, Any] = field(default_factory=dict)
    required_values: dict[str, Any] = field(default_factory=dict)
    observe_fields: list[str] = field(default_factory=list)
    persistence: str = "explicit_commit"
    before: dict[str, Any] = field(default_factory=dict)
    after: dict[str, Any] = field(default_factory=dict)
    applied: bool = False


@dataclass
class CodingRunResult:
    ok: bool
    return_value: Any = None
    trace: list[TraceEvent] = field(default_factory=list)
    writes: list[WriteEvent] = field(default_factory=list)
    final_state: dict[str, dict[str, Any]] = field(default_factory=dict)
    error: str = ""
    timed_out: bool = False


@dataclass
class CodingAttempt:
    source: str
    diagnostics: list[CodeDiagnostic] = field(default_factory=list)
    run: CodingRunResult | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0


@dataclass(frozen=True)
class CodingReview:
    text: str
    approved: bool
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0


@dataclass
class CodingPlan:
    goal: str
    source: str
    attempts: list[CodingAttempt] = field(default_factory=list)
    review: CodingReview | None = None
    reviews: list[CodingReview] = field(default_factory=list)

    @property
    def executable(self) -> bool:
        if not self.attempts:
            return False
        attempt = self.attempts[-1]
        return not attempt.diagnostics and (attempt.run is None or attempt.run.ok)

    @property
    def repaired(self) -> bool:
        return len(self.attempts) > 1

    @property
    def requirements_satisfied(self) -> bool:
        """Whether the reviewer accepted or produced the executable result."""
        return bool(self.executable and self.review is not None)


__all__ = [
    "CodeDiagnostic",
    "CodingAttempt",
    "CodingPlan",
    "CodingReview",
    "CodingRunResult",
    "TraceEvent",
    "WriteEvent",
]
