"""Program-level runtime state: the sole owner of statement scheduling.

ProgramRuntime owns cursor, env/run_log (via Interpreter), recovery budgets, and
task-level replan counts. It does not drive GUI turns — that stays in the agent
loop + InteractiveStatementExecutor.

The agent loop must not keep a parallel cursor (``_cur_run`` / ``_run_idx`` /
``_kickback_replans``); all statement sequencing mutates this object.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Callable, Generator

from gui_agent.core.orchestrator.program import Program, RunLike
from gui_agent.core.orchestrator.recovery import (
    MAX_KICKBACK_REPLANS,
    RecoveryLedger,
)
from gui_agent.core.orchestrator.runner import Interpreter
from gui_agent.core.schemas import (
    EventJournal,
    PolicyTurn,
    ProgramRevisionEvent,
    RecoveryJournalEvent,
    StatementContract,
    StatementOutcome,
)


@dataclass
class ProgramRuntime:
    """Mutable program execution state shared across the agent loop.

    Ownership (exclusive):
    - program revision + interpreter (env / run_log)
    - current statement cursor (current / index / notes_mark)
    - RecoveryLedger and kickback replan budget
    """

    program: Program
    interpreter: Interpreter
    _steps: Generator[RunLike, StatementOutcome, str]
    current: RunLike | None = None
    index: int = 0
    notes_mark: int = 0
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
        collect_fn: Callable | None = None,
        subdecompose_fn: Callable | None = None,
        expand_fn: Callable | None = None,
        select_fn: Callable | None = None,
        recovery: RecoveryLedger | None = None,
        journal: EventJournal | None = None,
        _record_revision: bool = True,
    ) -> "ProgramRuntime":
        interp = Interpreter(
            program,
            collect_fn=collect_fn,
            subdecompose_fn=subdecompose_fn,
            expand_fn=expand_fn,
            select_fn=select_fn,
        )
        gen = interp.steps()
        try:
            current = next(gen)
            reply = None
        except StopIteration as exc:
            current = None
            reply = exc.value or ""
        runtime = cls(
            program=program,
            interpreter=interp,
            _steps=gen,
            current=current,
            index=0,
            _recovery=recovery or RecoveryLedger(),
            reply=reply,
            _journal=journal,
        )
        if journal is not None and _record_revision:
            journal.append_program(
                program.model_dump(mode="json"),
                action="start",
            )
        return runtime

    @classmethod
    def resume(
        cls,
        fallback_program: Program,
        journal: EventJournal,
        *,
        collect_fn: Callable | None = None,
        subdecompose_fn: Callable | None = None,
        expand_fn: Callable | None = None,
        select_fn: Callable | None = None,
    ) -> "ProgramRuntime":
        """Replay the ordered journal into a fresh interpreter and recovery ledger.

        A journal without a Program revision starts a new run. Otherwise the persisted Program,
        not a newly compiled candidate, is authoritative. Terminal statement outcomes advance the
        generator; replacement revisions reproduce kickback/data-source hot swaps; the last
        unterminated invocation remains active for StatementRuntime restoration.
        """
        revisions = journal.program_revisions
        if not revisions:
            return cls.start(
                fallback_program,
                collect_fn=collect_fn,
                subdecompose_fn=subdecompose_fn,
                expand_fn=expand_fn,
                select_fn=select_fn,
                journal=journal,
            )

        first = revisions[0]
        if first.action != "start":
            raise ValueError("EventJournal must begin with a start Program revision")
        runtime = cls.start(
            Program.model_validate(first.program),
            collect_fn=collect_fn,
            subdecompose_fn=subdecompose_fn,
            expand_fn=expand_fn,
            select_fn=select_fn,
            journal=journal,
            _record_revision=False,
        )
        pending_terminal: tuple[StatementOutcome, str] | None = None
        active_instance_id = ""
        active_notes_mark = 0
        note_count = 0
        next_instance_notes_mark = 0
        seen_first_revision = False

        def apply_terminal() -> None:
            nonlocal pending_terminal, active_instance_id
            if pending_terminal is None:
                return
            outcome, instance_id = pending_terminal
            if runtime.current is None:
                raise ValueError(
                    f"journal terminal {instance_id!r} has no active Program statement"
                )
            runtime.current_instance_id = instance_id
            runtime.send_outcome(outcome)
            pending_terminal = None
            active_instance_id = ""

        for event in journal.events:
            if isinstance(event, ProgramRevisionEvent):
                if not seen_first_revision:
                    seen_first_revision = True
                    continue
                if event.action != "replace":
                    raise ValueError("only the first Program revision may use action='start'")
                if event.terminal_disposition == "record_then_drop":
                    apply_terminal()
                elif event.terminal_disposition == "abandon":
                    pending_terminal = None
                    active_instance_id = ""
                    runtime.current_instance_id = ""
                else:
                    apply_terminal()
                runtime.replace_program(
                    Program.model_validate(event.program),
                    collect_fn=collect_fn,
                    subdecompose_fn=subdecompose_fn,
                    expand_fn=expand_fn,
                    select_fn=select_fn,
                    drop_failed_from_log=(
                        event.terminal_disposition == "record_then_drop"
                    ),
                    _record_revision=False,
                )
                next_instance_notes_mark = note_count
                continue
            if isinstance(event, RecoveryJournalEvent):
                if (
                    event.mechanism == "tighten_return"
                    and event.outcome.startswith("tighten ")
                    and runtime.current is not None
                ):
                    runtime._recovery.next_attempt(runtime.index, runtime.current)
                if event.mechanism == "kickback_budget":
                    runtime._kickback_replans = max(
                        runtime._kickback_replans,
                        int(event.outcome or 0),
                    )
                runtime._recovery.record(
                    event.recovery_class,  # type: ignore[arg-type]
                    event.mechanism,
                    event.site,
                    detail=event.detail,
                    outcome=event.outcome,
                )
                continue
            if event.event_type == "content_note":
                note_count += 1
                continue
            if not isinstance(event, PolicyTurn):
                continue
            match = re.match(r"i(\d+):", event.statement_instance_id or "")
            if match:
                runtime._instance_seq = max(runtime._instance_seq, int(match.group(1)))
            if pending_terminal is not None:
                apply_terminal()
            if event.supervisor.outcome is not None:
                pending_terminal = (
                    event.supervisor.outcome,
                    event.statement_instance_id,
                )
                active_instance_id = ""
                # This terminal event closes the note interval for its invocation. Notes written
                # after it belong to the next statement even when they precede that statement's
                # first PolicyTurn, so retain the boundary instead of deriving it at first turn.
                next_instance_notes_mark = note_count
            elif event.statement_instance_id:
                if event.statement_instance_id != active_instance_id:
                    active_instance_id = event.statement_instance_id
                    active_notes_mark = next_instance_notes_mark

        apply_terminal()
        if active_instance_id:
            if runtime.current is None:
                raise ValueError(
                    f"journal active instance {active_instance_id!r} has no Program statement"
                )
            runtime.current_instance_id = active_instance_id
            runtime.notes_mark = active_notes_mark
        return runtime

    def next_instance_id(self, statement_id: str = "") -> str:
        """Monotonic instance id for one statement invocation (foreach-safe)."""
        if self.current_instance_id:
            raise RuntimeError(
                f"statement instance {self.current_instance_id!r} is still active"
            )
        self._instance_seq += 1
        suffix = statement_id or "stmt"
        iid = f"i{self._instance_seq}:{suffix}"
        self.current_instance_id = iid
        return iid

    @property
    def finished(self) -> bool:
        return self.current is None and self.reply is not None

    def send_outcome(self, outcome: StatementOutcome) -> RunLike | None:
        """Resume the interpreter with one authoritative statement outcome.

        The first run-log record appended while resuming is the record for the
        statement that just returned.  Later records may be interpreter-owned
        aggregates (for example the enclosing foreach materialization), so the
        invocation id must be attached by position rather than to ``run_log[-1]``.
        """
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
        """Atomically reserve one task-level replan attempt."""
        if self._kickback_replans >= MAX_KICKBACK_REPLANS:
            return None
        self._kickback_replans += 1
        site = str(
            getattr(self.current, "var", "")
            or getattr(self.current, "name", "")
            or "program"
        )
        self.record_recovery(
            "infeasible_route",
            "kickback_budget",
            site,
            outcome=str(self._kickback_replans),
        )
        return self._kickback_replans

    def next_return_attempt(self) -> int | None:
        if self.current is None:
            raise RuntimeError("cannot recover returns without an active statement")
        return self._recovery.next_attempt(self.index, self.current)

    def record_recovery(
        self,
        cls: str,
        mechanism: str,
        site: str,
        *,
        detail: str = "",
        outcome: str = "",
    ) -> None:
        self._recovery.record(
            cls,
            mechanism,
            site,
            detail=detail,
            outcome=outcome,
        )
        if self._journal is not None:
            self._journal.append_recovery(
                cls,
                mechanism,
                str(site or ""),
                detail=detail,
                outcome=outcome,
            )

    @property
    def has_recovery(self) -> bool:
        return bool(self._recovery.events)

    def recovery_summary(self) -> dict[str, Any]:
        return self._recovery.summary()

    def mark_notes(self, note_count: int) -> None:
        """Record content_notes length at the start of the current statement."""
        self.notes_mark = note_count

    def retry_current(self, statement: RunLike) -> None:
        """Replace the active statement contract without advancing the Program cursor."""
        if self.current is None:
            raise RuntimeError("cannot retry without an active statement")
        self.current = statement

    def restore_current_contract(self, contract: StatementContract) -> None:
        """Reapply a persisted return-tightened contract to the active Run."""
        if self.current is None:
            raise RuntimeError("cannot restore a contract without an active statement")
        update = {
            "name": contract.name,
            "success_condition": contract.success_condition,
            "returns": list(contract.returns),
            "read_spec": contract.read_spec,
        }
        for field_name in (
            "precondition",
            "effect_mode",
            "persistence",
            "target_controls",
            "target_values",
        ):
            if field_name in type(self.current).model_fields:
                update[field_name] = getattr(contract, field_name)
        self.current = self.current.model_copy(update=update)

    def replace_program(
        self,
        program: Program,
        *,
        collect_fn: Callable | None = None,
        subdecompose_fn: Callable | None = None,
        expand_fn: Callable | None = None,
        select_fn: Callable | None = None,
        inherit_env: bool = True,
        inherit_run_log: bool = True,
        drop_failed_from_log: bool = False,
        reason: str = "",
        terminal_disposition: str = "none",
        _record_revision: bool = True,
    ) -> None:
        """Hot-swap after redecompose; optionally keep prior env/run_log.

        When ``drop_failed_from_log`` is true (kickback recovery), superseded
        failed records are dropped so ``interp.failed`` does not permanently
        veto a successful retry.
        """
        prev_env = dict(self.interpreter.env) if inherit_env else {}
        prev_log = list(self.interpreter.run_log) if inherit_run_log else []
        if drop_failed_from_log:
            prev_log = [record for record in prev_log if record.result.is_completed]
        self.program = program
        self.interpreter = Interpreter(
            program,
            collect_fn=collect_fn,
            subdecompose_fn=subdecompose_fn,
            expand_fn=expand_fn,
            select_fn=select_fn,
        )
        if inherit_env:
            self.interpreter.env = prev_env
        if inherit_run_log:
            self.interpreter.run_log = prev_log
        self._steps = self.interpreter.steps()
        try:
            self.current = next(self._steps)
            self.reply = None
        except StopIteration as exc:
            self.current = None
            self.reply = exc.value or ""
        self.index = 0
        self.notes_mark = 0
        self.current_instance_id = ""
        if self._journal is not None and _record_revision:
            self._journal.append_program(
                program.model_dump(mode="json"),
                action="replace",
                reason=reason,
                terminal_disposition=terminal_disposition,  # type: ignore[arg-type]
            )
