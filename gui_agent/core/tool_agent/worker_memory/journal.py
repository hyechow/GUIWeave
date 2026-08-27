"""Append-only events for one GUI Worker attempt."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Literal


FactType = Literal["observation", "evidence", "commitment"]
FactStatus = Literal[
    "active", "dispatched", "satisfied", "uncertain", "failed",
]
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
class TargetRef:
    """Runtime-owned identity for one local GUI target interaction."""

    ref: str
    anchor_frame_id: str
    anchor_surface_fingerprint: str
    label: str
    container_context: str
    point: tuple[float, float] | None = None


@dataclass(frozen=True)
class ActionReceipt:
    """One invocation result bound to the commitments active before dispatch."""

    tool: str
    args: dict[str, Any]
    outcome: ActionOutcome
    commitment_refs: tuple[str, ...] = ()
    executed: bool = False
    target_ref: str = ""
    state_target_ref: str = ""
    target: TargetRef | None = None
    clears_target: bool = False


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
    receipt: ActionReceipt | None = None
    feedback: str = ""
    target_ref: str = ""

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

    @property
    def executed_tools(self) -> set[str]:
        return {
            event.receipt.tool for event in self.events
            if event.receipt is not None and event.receipt.executed
        }

    @property
    def latest_action_receipt(self) -> ActionReceipt | None:
        return next((event.receipt for event in reversed(self.events) if event.receipt), None)

    @property
    def active_target(self) -> TargetRef | None:
        active: TargetRef | None = None
        for event in self.events:
            receipt = event.receipt
            if receipt is None:
                continue
            if receipt.target is not None:
                active = receipt.target
            if receipt.clears_target:
                active = None
        return active

    def active_fact_statements(self, *, frame_id: str = "") -> tuple[str, ...]:
        """Return current Runtime-authored facts needed by small safety checks."""

        latest = {
            event.fact_ref: event for event in self.events
            if event.kind == "memory_update" and event.fact_ref
        }
        return tuple(
            event.statement for event in sorted(latest.values(), key=lambda item: item.sequence)
            if event.statement
            and event.status in {"active", "dispatched"}
            and (event.lifetime != "frame" or event.frame_id == frame_id)
        )

    @property
    def active_commitment_refs(self) -> tuple[str, ...]:
        latest = {
            event.fact_ref: event for event in self.events
            if event.fact_type == "commitment" and event.origin == "runtime"
        }
        return tuple(
            event.fact_ref for event in sorted(latest.values(), key=lambda item: item.sequence)
            if event.status == "dispatched"
        )

    def record_runtime_input(
        self,
        *,
        key: str,
        statement: str,
        event_ref: str = "runtime-input:1",
    ) -> WorkerJournalEvent:
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
        ))

    def record_runtime_commitment(
        self,
        *,
        step: int,
        substep: int,
        frame_id: str,
        tool: str,
        statement: str,
        status: FactStatus = "dispatched",
    ) -> WorkerJournalEvent:
        """Record a Runtime-owned action-intent transition."""
        key = f"action_{step}_{substep}"
        return self._append(WorkerJournalEvent(
            event_ref=f"step:{step}.{substep}:commitment:{status}",
            kind="memory_update",
            fact_type="commitment",
            key=key,
            status=status,
            lifetime="attempt",
            statement=" ".join((statement or tool).split()),
            frame_id=frame_id,
            attempt_id=self.worker_id,
            origin="runtime",
        ))

    def settle_runtime_commitment(
        self,
        *,
        step: int,
        substep: int,
        frame_id: str,
        tool: str,
        statement: str,
        result: Any,
    ) -> WorkerJournalEvent:
        signal = result.get("target_signal") if isinstance(result, dict) else None
        signal = signal if isinstance(signal, dict) else {}
        if not isinstance(result, dict) or result.get("status") != "executed":
            status: FactStatus = "failed"
        elif signal.get("status") == "off_target":
            status = "failed"
        elif result.get("no_effect") is True:
            status = "uncertain"
        else:
            status = "satisfied"
        return self.record_runtime_commitment(
            step=step, substep=substep, frame_id=frame_id, tool=tool,
            statement=statement, status=status,
        )

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
        surface_fingerprint: str = "",
        state_target_ref: str = "",
    ) -> WorkerJournalEvent:
        from .policy import action_receipt_event

        active_target = self.active_target
        signal = result.get("target_signal") if isinstance(result, dict) else None
        signal = signal if isinstance(signal, dict) else {}
        context = str(signal.get("container_context") or "").strip()
        point = (
            (float(args["x"]), float(args["y"]))
            if all(isinstance(args.get(key), (int, float)) for key in ("x", "y"))
            else None
        )
        target: TargetRef | None = None
        same_active_target = bool(
            active_target is not None
            and active_target.anchor_surface_fingerprint == surface_fingerprint
            and active_target.container_context == context
            and (
                active_target.point == point
                or active_target.point is not None
                and point is not None
                and abs(active_target.point[0] - point[0]) <= 40
                and abs(active_target.point[1] - point[1]) <= 40
            )
        )
        if (
            isinstance(result, dict)
            and result.get("status") == "executed"
            and signal.get("status") == "on_target"
            and (context or point is not None)
            and (
                active_target is None
                or active_target.anchor_surface_fingerprint != surface_fingerprint
                or not same_active_target
            )
        ):
            suffix = f".{substep}" if substep is not None else ""
            target = TargetRef(
                ref=f"target:{step}{suffix}",
                anchor_frame_id=frame_id,
                anchor_surface_fingerprint=surface_fingerprint,
                label=str(signal.get("actual_element") or "").strip(),
                container_context=context,
                point=point,
            )
        inherited_target_ref = (
            target.ref if target is not None else active_target.ref if active_target else ""
        )
        recorded = self._append(action_receipt_event(
            worker_id=self.worker_id,
            step=step,
            substep=substep,
            frame_id=frame_id,
            tool=tool,
            args=args,
            result=result,
            commitment_refs=commitment_refs,
            target_ref=inherited_target_ref,
            state_target_ref=state_target_ref,
            target=target,
        ))
        receipt = recorded.receipt
        if (
            receipt is not None
            and receipt.executed
            and (
                (receipt.outcome.kind == "effect" and receipt.outcome.target)
                or (
                    tool == "back"
                    and receipt.outcome.kind == "invoked"
                    and isinstance(result, dict)
                    and result.get("no_effect") is False
                )
            )
        ):
            suffix = f"_{substep}" if substep is not None else ""
            intent = str(args.get("description") or tool).strip()
            statement = (
                f"Target verification accepted executed action: intent={intent!r}; "
                f"actual_target={receipt.outcome.target!r}."
                if receipt.outcome.target else
                f"Runtime confirmed executed action effect: intent={intent!r}; tool={tool!r}."
            )
            self._append(WorkerJournalEvent(
                event_ref=f"step:{step}{suffix}:verified-action",
                kind="memory_update",
                fact_type="evidence",
                key=f"verified_action_{step}{suffix}",
                status="active",
                lifetime="attempt",
                statement=statement,
                frame_id=frame_id,
                attempt_id=self.worker_id,
                origin="runtime",
                target_ref=receipt.target_ref,
            ))
        return recorded

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
