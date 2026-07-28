"""Typed acceptance comparison without parsing or control-flow decisions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Literal, TypeVar


AcceptanceStatus = Literal["met", "unmet", "unknown"]
T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class AcceptanceResult(Generic[T]):
    """Result of comparing a typed contract value with typed observed state."""

    status: AcceptanceStatus
    expected: T
    actual: T | None
    reason: str = ""


class AcceptanceMatcher:
    """Compare canonical structures; callers retain all control-flow authority."""

    @staticmethod
    def exact(
        expected: T,
        actual: T | None,
        *,
        evidence_complete: bool,
    ) -> AcceptanceResult[T]:
        if not evidence_complete or actual is None:
            return AcceptanceResult(
                status="unknown",
                expected=expected,
                actual=actual,
                reason="complete structured evidence is unavailable",
            )
        if expected == actual:
            return AcceptanceResult(
                status="met",
                expected=expected,
                actual=actual,
            )
        return AcceptanceResult(
            status="unmet",
            expected=expected,
            actual=actual,
            reason="canonical structures differ",
        )
