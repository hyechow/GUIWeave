"""Statement-local interactive runtime: the only live state for one invocation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from gui_agent.core.schemas import StatementContract, StatementRuntimeSnapshot


@dataclass(slots=True)
class StatementRuntimeState:
    """Mutable interactive state for exactly one statement invocation.

    Created by ``begin_statement``, destroyed by ``end_statement``. Program
    advancement always creates a fresh instance; return-contract tighten reuses
    the same instance via ``reset_for_return_retry``.
    """

    contract: StatementContract
    instance_id: str
    task_type: Literal["action", "analysis"] = "action"
    execution_scope: str = ""
    # StatementInfo is derived from ``contract`` when the first turn is recorded.
    statement_info_emitted: bool = False

    def scope_key(self) -> str:
        """Instance-prefixed execution scope for history isolation."""
        return f"{self.instance_id}/statement"

    def reset_for_return_retry(
        self,
        new_contract: StatementContract,
    ) -> None:
        """Reuse this invocation after return-contract tighten.

        Journal history remains the invocation memory; no parallel controller state is reset.
        """
        self.contract = new_contract

    def restore(self, snapshot: StatementRuntimeSnapshot) -> None:
        """Restore logical statement state from the latest journal turn projection."""
        self.execution_scope = snapshot.execution_scope or self.scope_key()
        self.statement_info_emitted = snapshot.statement_info_emitted
        self.task_type = snapshot.task_type
