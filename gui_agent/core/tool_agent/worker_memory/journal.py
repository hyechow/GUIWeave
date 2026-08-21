"""Append-only events for one GUI Worker attempt."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Literal

from gui_agent.core.tool_agent.contracts import WorkerState


FactType = Literal["observation", "evidence", "claim", "commitment"]
FactStatus = Literal["active", "retracted", "completed"]
FactLifetime = Literal["frame", "attempt", "task"]
OutcomeKind = Literal["invoked", "effect", "no_effect", "off_target", "failed"]


@dataclass(frozen=True)
class ActionOutcome:
    """Typed Runtime interpretation of one tool result."""

    kind: OutcomeKind
    action_type: str = ""
    target: str = ""
    detail: Any = None


@dataclass(frozen=True)
class ActionReceipt:
    """One invocation result bound to the commitments active before dispatch."""

    tool: str
    args: dict[str, Any]
    outcome: ActionOutcome
    commitment_refs: tuple[str, ...] = ()
    preserves_window: bool = False
    executed: bool = False


@dataclass(frozen=True)
class WorkerJournalEvent:
    """One immutable semantic, execution, or feedback event."""

    event_ref: str
    kind: Literal["memory_update", "action_receipt", "candidate_commit", "feedback"]
    sequence: int = 0
    fact_type: FactType | None = None
    key: str = ""
    status: FactStatus | None = None
    lifetime: FactLifetime | None = None
    statement: str = ""
    frame_id: str = ""
    attempt_id: str = ""
    origin: Literal["worker", "runtime"] = "runtime"
    requires_integration: bool = False
    depends_on: tuple[str, ...] = ()
    supersedes: str = ""
    receipt: ActionReceipt | None = None
    feedback: str = ""

    @property
    def fact_ref(self) -> str:
        return f"{self.fact_type}:{self.key}" if self.fact_type and self.key else ""


@dataclass
class WorkerJournal:
    """Ordered event store; semantic decisions live in reducer and policy."""

    worker_id: str
    events: list[WorkerJournalEvent] = field(default_factory=list)

    def _append(self, event: WorkerJournalEvent) -> WorkerJournalEvent:
        recorded = replace(event, sequence=len(self.events) + 1)
        self.events.append(recorded)
        return recorded

    def _latest_fact(self, fact_ref: str) -> WorkerJournalEvent | None:
        return next(
            (event for event in reversed(self.events) if event.kind == "memory_update"
             and event.fact_ref == fact_ref), None,
        )

    @property
    def executed_tools(self) -> set[str]:
        return {
            event.receipt.tool for event in self.events
            if event.receipt is not None and event.receipt.executed
        }

    def record_memory_updates(
        self,
        *,
        step: int,
        frame_id: str,
        state: WorkerState,
    ) -> tuple[WorkerJournalEvent, ...]:
        from .policy import memory_update_events

        pending = memory_update_events(
            tuple(self.events), worker_id=self.worker_id,
            step=step, frame_id=frame_id, state=state,
        )
        return tuple(self._append(event) for event in pending)

    def record_runtime_input(
        self,
        *,
        key: str,
        statement: str,
        event_ref: str = "runtime-input:1",
        requires_integration: bool = False,
    ) -> WorkerJournalEvent:
        prior = self._latest_fact(f"evidence:{key}")
        return self._append(WorkerJournalEvent(
            event_ref=event_ref,
            kind="memory_update",
            fact_type="evidence",
            key=key,
            status="active",
            lifetime="task",
            statement=" ".join(statement.split()),
            attempt_id=self.worker_id,
            origin="runtime",
            requires_integration=requires_integration,
            supersedes=prior.event_ref if prior is not None else "",
        ))

    def record_runtime_result(
        self,
        *,
        step: int,
        result: Any,
        substep: int | None = None,
    ) -> WorkerJournalEvent | None:
        if not isinstance(result, dict):
            return None
        statement = str(result.get("_runtime_memory_statement") or "").strip()
        if not statement:
            return None
        suffix = f"_{substep}" if substep is not None else ""
        step_ref = f"{step}.{substep}" if substep is not None else str(step)
        return self.record_runtime_input(
            key=f"user_response_{step}{suffix}",
            statement=statement,
            event_ref=f"step:{step_ref}:runtime-input",
            requires_integration=True,
        )

    def record_action_result(
        self,
        *,
        step: int,
        frame_id: str,
        tool: str,
        args: dict[str, Any],
        result: Any,
        substep: int | None = None,
        commitment_refs: tuple[str, ...] = (),
    ) -> WorkerJournalEvent:
        from .policy import action_receipt_event

        return self._append(action_receipt_event(
            worker_id=self.worker_id,
            step=step,
            substep=substep,
            frame_id=frame_id,
            tool=tool,
            args=args,
            result=result,
            commitment_refs=commitment_refs,
        ))

    def record_guard(
        self,
        *,
        step: int,
        repair_turn: int,
        tool: str,
        reason: str,
    ) -> None:
        self._append(WorkerJournalEvent(
            event_ref=f"step:{step}:guard:{repair_turn}",
            kind="feedback",
            attempt_id=self.worker_id,
            feedback=f"tool={tool}; {reason}",
        ))
