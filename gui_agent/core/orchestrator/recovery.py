"""Program-level recovery budgets and append-only accounting."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


RecoveryClass = Literal[
    "compile_error",
    "contract_violation",
    "infeasible_route",
    "data_source_error",
]

MAX_EMPTY_RETURN_RECOVERIES = 3
MAX_KICKBACK_REPLANS = 1


@dataclass(frozen=True)
class RecoveryEvent:
    cls: RecoveryClass
    mechanism: str
    site: str
    detail: str = ""
    outcome: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "class": self.cls,
            "mechanism": self.mechanism,
            "site": self.site,
            "detail": self.detail,
            "outcome": self.outcome,
        }


class ReturnRecoveryLedger:
    """Bound missing-output retries by executor call site."""

    def __init__(self, max_attempts: int = MAX_EMPTY_RETURN_RECOVERIES) -> None:
        self.max_attempts = max_attempts
        self._attempts: dict[tuple[int, str, tuple[str, ...]], int] = {}

    @staticmethod
    def _key(index: int, statement: object) -> tuple[int, str, tuple[str, ...]]:
        returns = getattr(statement, "returns", {})
        return (
            index,
            str(getattr(statement, "id", "") or getattr(statement, "bind", "")),
            tuple(str(field) for field in returns),
        )

    def next_attempt(self, index: int, statement: object) -> int | None:
        key = self._key(index, statement)
        attempt = self._attempts.get(key, 0) + 1
        if attempt > self.max_attempts:
            return None
        self._attempts[key] = attempt
        return attempt


class RecoveryLedger(ReturnRecoveryLedger):
    """The one Program recovery budget and event ledger."""

    def __init__(self, max_attempts: int = MAX_EMPTY_RETURN_RECOVERIES) -> None:
        super().__init__(max_attempts=max_attempts)
        self.events: list[RecoveryEvent] = []

    def record(
        self,
        cls: RecoveryClass,
        mechanism: str,
        site: str,
        *,
        detail: str = "",
        outcome: str = "",
    ) -> RecoveryEvent:
        event = RecoveryEvent(cls, mechanism, str(site or ""), detail, outcome)
        self.events.append(event)
        return event

    def summary(self) -> dict:
        by_class: dict[str, int] = {}
        by_mechanism: dict[str, int] = {}
        for event in self.events:
            by_class[event.cls] = by_class.get(event.cls, 0) + 1
            by_mechanism[event.mechanism] = by_mechanism.get(event.mechanism, 0) + 1
        return {
            "total": len(self.events),
            "by_class": by_class,
            "by_mechanism": by_mechanism,
            "events": [event.to_dict() for event in self.events],
        }


__all__ = [
    "MAX_EMPTY_RETURN_RECOVERIES",
    "MAX_KICKBACK_REPLANS",
    "RecoveryClass",
    "RecoveryEvent",
    "RecoveryLedger",
    "ReturnRecoveryLedger",
]
