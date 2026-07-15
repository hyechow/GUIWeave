"""Program-level runtime state: the sole owner of statement scheduling.

ProgramRuntime owns cursor, env/run_log (via Interpreter), recovery budgets, and
task-level replan counts. It does not drive GUI turns — that stays in the agent
loop + InteractiveStatementExecutor (supervisor reseed path).

The agent loop must not keep a parallel cursor (``_cur_run`` / ``_run_idx`` /
``_kickback_replans``); all statement sequencing mutates this object.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Generator

from gui_agent.core.orchestrator.program import Program, RunLike, RunResult
from gui_agent.core.orchestrator.recovery import (
    MAX_KICKBACK_REPLANS,
    RecoveryLedger,
)
from gui_agent.core.orchestrator.runner import Interpreter

if TYPE_CHECKING:
    from gui_agent.core.run.statements.outcome import StatementOutcome


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
    steps: Generator[RunLike, RunResult, str]
    current: RunLike | None = None
    index: int = 0
    notes_mark: int = 0
    recovery: RecoveryLedger = field(default_factory=RecoveryLedger)
    kickback_replans: int = 0
    reply: str | None = None

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
        return cls(
            program=program,
            interpreter=interp,
            steps=gen,
            current=current,
            index=0,
            recovery=recovery or RecoveryLedger(),
            reply=reply,
        )

    @property
    def finished(self) -> bool:
        return self.current is None and self.reply is not None

    def send(self, result: RunResult) -> RunLike | None:
        """Resume the interpreter with a statement result; update cursor."""
        try:
            self.current = self.steps.send(result)
            self.index += 1
            return self.current
        except StopIteration as exc:
            self.current = None
            self.reply = exc.value or ""
            return None

    def send_outcome(self, outcome: "StatementOutcome") -> RunLike | None:
        """Resume from a terminal StatementOutcome (never a mid-loop decision)."""
        return self.send(outcome.to_run_result())

    def can_kickback(self) -> bool:
        return self.kickback_replans < MAX_KICKBACK_REPLANS

    def record_kickback(self) -> int:
        self.kickback_replans += 1
        return self.kickback_replans

    def mark_notes(self, note_count: int) -> None:
        """Record content_notes length at the start of the current statement."""
        self.notes_mark = note_count

    def accept_dispatch_cursor(
        self,
        *,
        current: RunLike | None,
        index: int,
        notes_mark: int | None = None,
    ) -> None:
        """Install cursor after the immediate dispatcher advanced the generator.

        The dispatcher alone may ``send`` on ``self.steps``; it reports the new
        cursor here so ProgramRuntime remains the single owner of the fields.
        """
        self.current = current
        self.index = index
        if notes_mark is not None:
            self.notes_mark = notes_mark

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
    ) -> None:
        """Hot-swap after redecompose; optionally keep prior env/run_log.

        When ``drop_failed_from_log`` is true (kickback recovery), superseded
        failed records are dropped so ``interp.failed`` does not permanently
        veto a successful retry.
        """
        prev_env = dict(self.interpreter.env) if inherit_env else {}
        prev_log = list(self.interpreter.run_log) if inherit_run_log else []
        if drop_failed_from_log:
            prev_log = [record for record in prev_log if not record.result.failed]
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
        self.steps = self.interpreter.steps()
        try:
            self.current = next(self.steps)
            self.reply = None
        except StopIteration as exc:
            self.current = None
            self.reply = exc.value or ""
        self.index = 0
        self.notes_mark = 0
