"""Pure routing for Program-level statement recovery decisions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from gui_agent.core.orchestrator.recovery import RecoveryClass
from gui_agent.core.schemas import StatementOutcome


RecoveryAction = Literal[
    "advance_program",
    "tighten_return",
    "kickback",
    "fail_or_escalate",
]


@dataclass(frozen=True)
class RecoveryDecision:
    """One control decision; budgets and mutable recovery state stay in ProgramRuntime."""

    action: RecoveryAction
    recovery_class: RecoveryClass | None = None


class RecoveryRouter:
    """Make Program recovery authority explicit without owning any runtime state."""

    @staticmethod
    def route_statement(
        outcome: StatementOutcome,
        *,
        return_violation: bool = False,
        can_redecompose: bool = False,
    ) -> RecoveryDecision:
        if outcome.phase == "infeasible" and can_redecompose:
            return RecoveryDecision("kickback", "infeasible_route")
        if outcome.is_completed and return_violation:
            return RecoveryDecision("tighten_return", "contract_violation")
        if outcome.is_completed:
            return RecoveryDecision("advance_program")
        return RecoveryDecision("fail_or_escalate")

    @staticmethod
    def route_program_end(
        *,
        failure_evidence: str | None,
        can_redecompose: bool,
    ) -> RecoveryDecision:
        if failure_evidence and can_redecompose:
            return RecoveryDecision("kickback", "data_source_error")
        return RecoveryDecision("fail_or_escalate")


__all__ = ["RecoveryAction", "RecoveryDecision", "RecoveryRouter"]
