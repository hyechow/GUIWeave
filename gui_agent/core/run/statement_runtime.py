"""Statement-local interactive runtime: the only live state for one invocation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from gui_agent.core.run.execution_signals import (
    ConstraintEntry,
    ConstraintLedger,
    ExecutionContract,
)
from gui_agent.core.run.progress_monitor import ProgressMonitor, TraceStep
from gui_agent.core.schemas import StatementContract, StatementRuntimeSnapshot
from gui_agent.core.supervisor.statement.acquisition import TargetAcquireController


@dataclass
class StatementRuntimeState:
    """Mutable interactive state for exactly one statement invocation.

    Created by ``begin_statement``, destroyed by ``end_statement``. Program
    advancement always creates a fresh instance; return-contract tighten reuses
    the same instance via ``reset_for_return_retry``.
    """

    contract: StatementContract
    instance_id: str
    execution_contract: ExecutionContract
    task_type: Literal["action", "analysis"] = "action"
    retry_count: int = 0
    early_feasibility_probed: bool = False
    scroll_count: int = 0
    execution_scope: str = ""
    last_page_identity: str = ""
    last_check: Any = None
    done_check: Any = None
    monitor: ProgressMonitor = field(default_factory=ProgressMonitor)
    constraint_ledger: ConstraintLedger = field(default_factory=ConstraintLedger)
    target_acquire: TargetAcquireController = field(
        default_factory=TargetAcquireController
    )
    skip_initial_check: bool = False
    # StatementInfo DTO built from the contract at begin time; written onto the FIRST turn of
    # this invocation only. `statement_info_emitted` flips True after that first write so later
    # turns (and the terminal observation turn) carry statement=None but the same instance_id.
    statement_info: Any = None
    statement_info_emitted: bool = False

    def scope_key(self, *, row_identity: str = "") -> str:
        """Instance-prefixed execution scope for history isolation."""
        if row_identity:
            return f"{self.instance_id}/row:{row_identity}"
        return f"{self.instance_id}/statement"

    def reset_for_return_retry(
        self,
        new_contract: StatementContract,
        execution_contract: ExecutionContract,
    ) -> None:
        """Reuse this invocation after return-contract tighten.

        Keeps action-trace / runtime constraints / acquire session. Clears
        observation windows and UI retry counters so the next check is fresh.
        """
        self.contract = new_contract
        self.execution_contract = execution_contract
        self.retry_count = 0
        self.early_feasibility_probed = False
        self.scroll_count = 0
        self.last_page_identity = ""
        self.last_check = None
        self.done_check = None
        self.skip_initial_check = False
        self.monitor.reset_for_retry()

    def restore(self, snapshot: StatementRuntimeSnapshot) -> None:
        """Restore logical statement state from the latest journal turn projection."""
        self.retry_count = snapshot.retry_count
        self.early_feasibility_probed = snapshot.early_feasibility_probed
        self.scroll_count = snapshot.scroll_count
        self.execution_scope = snapshot.execution_scope or self.scope_key()
        self.last_page_identity = snapshot.last_page_identity
        self.skip_initial_check = snapshot.skip_initial_check
        self.statement_info_emitted = snapshot.statement_info_emitted
        self.task_type = snapshot.task_type
        self.constraint_ledger.entries = [
            ConstraintEntry(
                text=entry.text,
                scope=entry.scope,
                source=entry.source,
            )
            for entry in snapshot.constraints
        ]
        self.monitor.turns = [
            TraceStep(
                index=trace.index,
                state=trace.state,
                decision=trace.decision,
                interaction_state=trace.interaction_state,
                scope=trace.scope,
            )
            for trace in snapshot.progress_trace
        ]
        self.monitor._progress_values = list(snapshot.progress_values)
        self.monitor._last_url = snapshot.last_url
        self.monitor._last_dom_state = snapshot.last_dom_state
        self.monitor.url_changed = False
        self.monitor.dom_changed = False
