"""Static turn-budget estimate for the semantic Program IR."""

from __future__ import annotations

from .program import Acquire, Command, Compute, Data, Finish, ForEach, If, Interact, Program, Stmt


def _statement_turns(statement: Stmt) -> int:
    if isinstance(statement, Interact):
        return 4
    if isinstance(statement, (Acquire, Data, Compute, Command)):
        return 1
    if isinstance(statement, If):
        return max(
            sum(_statement_turns(item) for item in statement.then),
            sum(_statement_turns(item) for item in statement.otherwise),
        )
    if isinstance(statement, ForEach):
        # Cardinality is runtime data. Reserve two representative iterations;
        # the caller's floor remains the hard lower bound.
        return 2 * sum(_statement_turns(item) for item in statement.body)
    if isinstance(statement, Finish):
        return 0
    return 0


def estimate_program_turns(program: Program, *, floor: int = 0) -> int:
    """Return a conservative static budget without predicting UI phases."""
    estimated = sum(_statement_turns(item) for item in program.statements)
    return max(int(floor), estimated)


__all__ = ["estimate_program_turns"]
