"""Program-level runtime state and journal replay.

``ProgramRuntime`` is the sole owner of interpreter progress, typed env, run
log and Program-level recovery.  Executors only consume the current resolved
``StatementInvocation`` and return a ``StatementOutcome``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Generator

from gui_agent.core.orchestrator.program import Interact, Program
from gui_agent.core.orchestrator.recovery import MAX_KICKBACK_REPLANS, RecoveryLedger
from gui_agent.core.orchestrator.runner import Interpreter, StatementInvocation
from gui_agent.core.schemas import (
    AcquisitionReceiptEvent,
    CollectionSliceEvent,
    EventJournal,
    PolicyTurn,
    ProgramRevisionEvent,
    RecoveryJournalEvent,
    StatementContract,
    StatementOutcome,
    StatementOutcomeEvent,
)


@dataclass
class ProgramRuntime:
    program: Program
    interpreter: Interpreter
    _steps: Generator[StatementInvocation, StatementOutcome, str]
    current: StatementInvocation | None = None
    index: int = 0
    _recovery: RecoveryLedger = field(default_factory=RecoveryLedger)
    _kickback_replans: int = 0
    reply: str | None = None
    _instance_seq: int = 0
    current_instance_id: str = ""
    _journal: EventJournal | None = None

    @classmethod
    def start(
        cls,
        program: Program,
        *,
        recovery: RecoveryLedger | None = None,
        journal: EventJournal | None = None,
        _record_revision: bool = True,
    ) -> "ProgramRuntime":
        interpreter = Interpreter(program)
        steps = interpreter.steps()
        try:
            current = next(steps)
            reply = None
        except StopIteration as exc:
            current = None
            reply = exc.value or ""
        runtime = cls(
            program=program,
            interpreter=interpreter,
            _steps=steps,
            current=current,
            _recovery=recovery or RecoveryLedger(),
            reply=reply,
            _journal=journal,
        )
        if journal is not None and _record_revision:
            journal.append_program(program.model_dump(mode="json"), action="start")
        return runtime

    @classmethod
    def resume(
        cls,
        fallback_program: Program,
        journal: EventJournal,
    ) -> "ProgramRuntime":
        """Replay Program revisions and terminal outcomes from the new journal."""

        revisions = journal.program_revisions
        if not revisions:
            if journal.events:
                raise ValueError(
                    "EventJournal with execution events requires an initial Program revision"
                )
            return cls.start(fallback_program, journal=journal)
        if revisions[0].action != "start":
            raise ValueError("EventJournal must begin with a start Program revision")
        runtime = cls.start(
            Program.model_validate(revisions[0].program),
            journal=journal,
            _record_revision=False,
        )
        pending: tuple[StatementOutcome, str] | None = None
        active_instance = ""
        seen_start = False

        def apply_terminal() -> None:
            nonlocal pending, active_instance
            if pending is None:
                return
            outcome, instance_id = pending
            if runtime.current is None:
                raise ValueError(f"journal terminal {instance_id!r} has no active statement")
            runtime.current_instance_id = instance_id
            runtime.send_outcome(outcome)
            pending = None
            active_instance = ""

        for event in journal.events:
            if isinstance(event, ProgramRevisionEvent):
                if not seen_start:
                    seen_start = True
                    continue
                if event.action != "replace":
                    raise ValueError("only first Program revision may use action='start'")
                if event.terminal_disposition == "record_then_drop":
                    apply_terminal()
                elif event.terminal_disposition == "abandon":
                    pending = None
                    active_instance = ""
                    runtime.current_instance_id = ""
                else:
                    apply_terminal()
                runtime.replace_program(
                    Program.model_validate(event.program),
                    drop_failed_from_log=event.terminal_disposition == "record_then_drop",
                    _record_revision=False,
                )
                continue
            if isinstance(event, RecoveryJournalEvent):
                if event.mechanism == "kickback_budget":
                    runtime._kickback_replans = max(
                        runtime._kickback_replans, int(event.outcome or 0)
                    )
                runtime._recovery.record(
                    event.recovery_class,  # type: ignore[arg-type]
                    event.mechanism,
                    event.site,
                    detail=event.detail,
                    outcome=event.outcome,
                )
                continue
            if isinstance(event, (CollectionSliceEvent, AcquisitionReceiptEvent)):
                if pending is not None:
                    apply_terminal()
                match = re.match(r"i(\d+):", event.statement_instance_id or "")
                if match:
                    runtime._instance_seq = max(
                        runtime._instance_seq, int(match.group(1))
                    )
                if event.statement_instance_id != active_instance:
                    active_instance = event.statement_instance_id
                continue
            if isinstance(event, StatementOutcomeEvent):
                match = re.match(r"i(\d+):", event.statement_instance_id or "")
                if match:
                    runtime._instance_seq = max(runtime._instance_seq, int(match.group(1)))
                if pending is not None:
                    apply_terminal()
                pending = (event.outcome, event.statement_instance_id)
                active_instance = ""
                continue
            if not isinstance(event, PolicyTurn):
                continue
            if pending is not None:
                apply_terminal()
            match = re.match(r"i(\d+):", event.statement_instance_id or "")
            if match:
                runtime._instance_seq = max(runtime._instance_seq, int(match.group(1)))
            if event.statement_instance_id and event.statement_instance_id != active_instance:
                active_instance = event.statement_instance_id

        apply_terminal()
        if active_instance:
            if runtime.current is None:
                raise ValueError(f"journal active instance {active_instance!r} has no statement")
            runtime.current_instance_id = active_instance
        return runtime

    @property
    def finished(self) -> bool:
        return self.current is None and self.reply is not None

    def next_instance_id(self, statement_id: str = "") -> str:
        if self.current_instance_id:
            raise RuntimeError(f"statement instance {self.current_instance_id!r} is still active")
        self._instance_seq += 1
        self.current_instance_id = f"i{self._instance_seq}:{statement_id or 'stmt'}"
        return self.current_instance_id

    def send_outcome(self, outcome: StatementOutcome) -> StatementInvocation | None:
        log_start = len(self.interpreter.run_log)
        instance_id = self.current_instance_id
        try:
            self.current = self._steps.send(outcome)
            self.index += 1
            return self.current
        except StopIteration as exc:
            self.current = None
            self.reply = exc.value or ""
            return None
        finally:
            if instance_id and len(self.interpreter.run_log) > log_start:
                self.interpreter.run_log[log_start].instance_id = instance_id
            self.current_instance_id = ""

    def begin_kickback(self) -> int | None:
        if self._kickback_replans >= MAX_KICKBACK_REPLANS:
            return None
        self._kickback_replans += 1
        site = self.current.id if self.current else "program"
        self.record_recovery(
            "infeasible_route",
            "kickback_budget",
            site,
            outcome=str(self._kickback_replans),
        )
        return self._kickback_replans

    def record_recovery(
        self,
        cls: str,
        mechanism: str,
        site: str,
        *,
        detail: str = "",
        outcome: str = "",
    ) -> None:
        self._recovery.record(cls, mechanism, site, detail=detail, outcome=outcome)
        if self._journal is not None:
            self._journal.append_recovery(
                cls, mechanism, site, detail=detail, outcome=outcome
            )

    @property
    def has_recovery(self) -> bool:
        return bool(self._recovery.events)

    def recovery_summary(self) -> dict[str, Any]:
        return self._recovery.summary()

    def retry_current(self, invocation: StatementInvocation) -> None:
        if self.current is None:
            raise RuntimeError("cannot retry without an active statement")
        self.current = invocation

    def restore_current_contract(self, contract: StatementContract) -> None:
        """Restore an active Interact contract from a persisted live turn."""
        if self.current is None or not isinstance(self.current.statement, Interact):
            raise RuntimeError("only an active Interact has a restorable contract")
        statement = self.current.statement.model_copy(
            update={
                "goal": contract.goal,
                "success": contract.success,
            }
        )
        self.current = self.current.model_copy(update={"statement": statement})

    def replace_program(
        self,
        program: Program,
        *,
        inherit_env: bool = True,
        inherit_run_log: bool = True,
        drop_failed_from_log: bool = False,
        reason: str = "",
        terminal_disposition: str = "none",
        _record_revision: bool = True,
    ) -> None:
        prev_env = dict(self.interpreter.env) if inherit_env else {}
        prev_verifications = (
            dict(self.interpreter.binding_verifications) if inherit_env else {}
        )
        prev_log = list(self.interpreter.run_log) if inherit_run_log else []
        prev_contracts = (
            dict(self.interpreter.binding_contracts) if inherit_env else {}
        )
        if drop_failed_from_log:
            prev_log = [record for record in prev_log if record.result.is_completed]
        self.program = program
        self.interpreter = Interpreter(program, inherited_contracts=prev_contracts)
        self.interpreter.env = prev_env
        self.interpreter.binding_verifications = prev_verifications
        self.interpreter.run_log = prev_log
        self._steps = self.interpreter.steps()
        try:
            self.current = next(self._steps)
            self.reply = None
        except StopIteration as exc:
            self.current = None
            self.reply = exc.value or ""
        self.index = 0
        self.current_instance_id = ""
        if self._journal is not None and _record_revision:
            self._journal.append_program(
                program.model_dump(mode="json"),
                action="replace",
                reason=reason,
                terminal_disposition=terminal_disposition,  # type: ignore[arg-type]
            )
