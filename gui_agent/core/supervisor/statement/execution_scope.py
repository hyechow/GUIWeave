"""Call-frame scope for one linear Interact execution."""

from __future__ import annotations

from gui_agent.core.schemas import Observation, PolicyTurn, StatementContract


def execution_scope_for(
    statement: StatementContract,
    observation: Observation,
    *,
    instance_id: str,
) -> str:
    """The invocation is the only statement-memory scope."""
    del statement, observation
    return f"{instance_id}/statement"


def history_for_scope(
    history: list[PolicyTurn],
    statement: StatementContract,
    observation: Observation,
    *,
    instance_id: str,
) -> list[PolicyTurn]:
    del statement, observation
    return [
        turn for turn in history
        if turn.statement_instance_id == instance_id
    ]


__all__ = ["execution_scope_for", "history_for_scope"]
