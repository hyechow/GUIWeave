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
    _steps: Generator[RunLike, RunResult, str]
    current: RunLike | None = None
    index: int = 0
    notes_mark: int = 0
    recovery: RecoveryLedger = field(default_factory=RecoveryLedger)
    kickback_replans: int = 0
    reply: str | None = None
    _instance_seq: int = 0
    current_instance_id: str = ""

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
            _steps=gen,
            current=current,
            index=0,
            recovery=recovery or RecoveryLedger(),
            reply=reply,
        )

    def next_instance_id(self, statement_id: str = "") -> str:
        """Monotonic instance id for one statement invocation (foreach-safe)."""
        self._instance_seq += 1
        suffix = statement_id or "stmt"
        iid = f"i{self._instance_seq}:{suffix}"
        self.current_instance_id = iid
        return iid

    @property
    def finished(self) -> bool:
        return self.current is None and self.reply is not None

    def _send_result(self, result: RunResult) -> RunLike | None:
        """Resume the interpreter wire protocol and update the owned cursor."""
        try:
            self.current = self._steps.send(result)
            self.index += 1
            return self.current
        except StopIteration as exc:
            self.current = None
            self.reply = exc.value or ""
            return None

    def send_outcome(self, outcome: "StatementOutcome") -> RunLike | None:
        """Resume from one terminal statement outcome."""
        return self._send_result(outcome.to_run_result())

    def can_kickback(self) -> bool:
        return self.kickback_replans < MAX_KICKBACK_REPLANS

    def record_kickback(self) -> int:
        self.kickback_replans += 1
        return self.kickback_replans

    def mark_notes(self, note_count: int) -> None:
        """Record content_notes length at the start of the current statement."""
        self.notes_mark = note_count

    def retry_current(self, statement: RunLike) -> None:
        """Replace the active statement contract without advancing the Program cursor."""
        if self.current is None:
            raise RuntimeError("cannot retry without an active statement")
        self.current = statement

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
        self._steps = self.interpreter.steps()
        try:
            self.current = next(self._steps)
            self.reply = None
        except StopIteration as exc:
            self.current = None
            self.reply = exc.value or ""
        self.index = 0
        self.notes_mark = 0
