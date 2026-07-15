"""Program-level runtime state: the sole owner of statement scheduling.

ProgramRuntime owns cursor, env/run_log (via Interpreter), recovery budgets, and
task-level replan counts. It does not drive GUI turns — that stays in the agent
loop + InteractiveStatementExecutor (supervisor reseed path).

DAG walkers and ``program is None`` fallbacks are not part of this surface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Generator, Optional

from gui_agent.core.orchestrator.program import Program, Run, RunLike, RunResult
from gui_agent.core.orchestrator.recovery import (
    MAX_KICKBACK_REPLANS,
    RecoveryLedger,
)
from gui_agent.core.orchestrator.runner import Interpreter


def compile_single_statement_program(goal: str) -> Program:
    """Deterministic compile when no orchestrator Program is supplied.

    One interactive action covering the whole goal. Multi-step structure requires
    the orchestrator decomposer — there is no silent DAG walker fallback.
    """
    text = (goal or "").strip() or "完成用户目标"
    return Program(
        goal=text,
        statements=[
            Run(
                name=text,
                kind="action",
                success_condition=f"完成用户目标：{text}",
            )
        ],
    )


def ensure_program(program: Program | None, goal: str) -> Program:
    """Return a Program; never None. Compiles a single-statement program if needed."""
    if program is not None:
        return program
    return compile_single_statement_program(goal)


@dataclass
class ProgramRuntime:
    """Mutable program execution state shared across the agent loop.

    Ownership (exclusive):
    - program revision + interpreter (env / run_log)
    - current statement cursor (_cur_run / index)
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

    def can_kickback(self) -> bool:
        return self.kickback_replans < MAX_KICKBACK_REPLANS

    def record_kickback(self) -> int:
        self.kickback_replans += 1
        return self.kickback_replans

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
    ) -> None:
        """Hot-swap after redecompose; optionally keep prior env/run_log."""
        prev_env = dict(self.interpreter.env) if inherit_env else {}
        prev_log = list(self.interpreter.run_log) if inherit_run_log else []
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
        except StopIteration as exc:
            self.current = None
            self.reply = exc.value or ""
        self.index = 0
        self.notes_mark = 0
