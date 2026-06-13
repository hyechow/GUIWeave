"""DSL interpreter = the milestone SEQUENCER (replaces the supervisor's walker/DAG).

The agent_loop IS the program runner: it owns the session + the turn loop, and each
milestone is a sub-loop (several turns driving one milestone to done). The DSL only
changes WHO decides the next milestone — the walker becomes this interpreter, which
threads each run's structured RunResult through variables / conditions / finish.

So the interpreter must be STEPPABLE, not a synchronous black box: executing a run() is
many turns of the engine, not one call. `Interpreter.steps()` is a generator — it yields
each Run for the engine to drive, receives the RunResult via send(), and returns the final
reply. Persisting every run's reads in env/run_log is why "the middle read it but the
output didn't know" disappears: the answer comes from the whole program, not the last loop.

    interp = Interpreter(program)
    gen = interp.steps()
    run = next(gen)
    while True:
        result = <drive milestone `run` to done>     # agent_loop sub-loop
        run = gen.send(result)                         # next Run
    # StopIteration.value = final reply

`drive()` / `ProgramRunner` are the synchronous convenience for tests + headless use.
"""

from __future__ import annotations

import re
from typing import Callable, Generator, Optional

from pydantic import BaseModel, Field

from .program import Cond, Finish, If, Program, Run, RunResult

# Drive one milestone (one Run spec) to a terminal state and return its structured result.
MilestoneExecutor = Callable[[Run], RunResult]

_TEMPLATE_RE = re.compile(r"\{(\w+)\[([^\]]+)\]\}")


class RunRecord(BaseModel):
    name: str
    var: Optional[str] = None
    result: RunResult


class OrchestratorResult(BaseModel):
    reply: str
    failed: bool = False
    env: dict[str, RunResult] = Field(default_factory=dict)
    run_log: list[RunRecord] = Field(default_factory=list)


class Interpreter:
    """Steppable DSL interpreter. Drive it via steps() (a generator): it yields each Run
    and receives its RunResult via send(); steps() returns the final reply. env / run_log
    accumulate so the answer is built from the whole program's structured state."""

    def __init__(self, program: Program) -> None:
        self._program = program
        self.env: dict[str, RunResult] = {}
        self.run_log: list[RunRecord] = []

    @property
    def failed(self) -> bool:
        return any(r.result.failed for r in self.run_log)

    def steps(self) -> Generator[Run, RunResult, str]:
        reply = yield from self._block(self._program.statements)
        return reply if reply is not None else self._auto_summary()

    def _block(self, stmts: list) -> Generator[Run, RunResult, Optional[str]]:
        """Execute a block; returns a terminal reply (finish / failure) or None if it ran
        to the end of the block without terminating."""
        for s in stmts:
            if isinstance(s, Run):
                result = yield s  # engine drives this milestone and send()s back its result
                self.run_log.append(RunRecord(name=s.name, var=s.var, result=result))
                if s.var:
                    self.env[s.var] = result
                if result.failed:
                    # MVP: a failed milestone stops the program and reports honestly.
                    return f"子任务「{s.name}」未能完成：{result.summary or '执行失败'}"
            elif isinstance(s, If):
                branch = s.then if self._eval(s.cond) else s.otherwise
                reply = yield from self._block(branch)
                if reply is not None:
                    return reply
            elif isinstance(s, Finish):
                return self._render(s.message)
        return None

    def _eval(self, cond: Cond) -> bool:
        rv = self.env.get(cond.var)
        actual = (rv.reads.get(cond.field, "") if rv else "").strip()
        target = cond.value.strip()
        return actual == target if cond.cmp == "==" else actual != target

    def _render(self, template: str) -> str:
        def _sub(m: "re.Match[str]") -> str:
            var, field = m.group(1), m.group(2).strip().strip("'\"")
            rv = self.env.get(var)
            return rv.reads.get(field, "") if rv else ""

        return _TEMPLATE_RE.sub(_sub, template)

    def _auto_summary(self) -> str:
        """No explicit finish(): summarize from the persisted run results (reads first, else
        the last run's summary) — the orchestrator answers from the program's whole state."""
        reads = [
            "；".join(f"{k}：{v}" for k, v in r.result.reads.items() if v)
            for r in self.run_log
            if r.result.reads
        ]
        reads = [p for p in reads if p]
        if reads:
            return "；".join(reads)
        return (self.run_log[-1].result.summary if self.run_log else "") or "任务已执行完毕。"


def drive(interp: Interpreter, execute: MilestoneExecutor) -> str:
    """Synchronously drive an interpreter with a callable executor (tests / headless use).
    The GUI agent_loop drives interp.steps() turn-by-turn instead of using this."""
    gen = interp.steps()
    try:
        run = next(gen)
    except StopIteration as e:  # program with no run() (e.g. just finish / empty)
        return e.value or ""
    while True:
        result = execute(run)
        try:
            run = gen.send(result)
        except StopIteration as e:
            return e.value or ""


class ProgramRunner:
    """Synchronous convenience runner (tests / headless): build an Interpreter and drive it
    with the injected executor. The GUI agent_loop instead drives Interpreter.steps()
    directly, executing each yielded Run as a milestone sub-loop within its open session."""

    def __init__(self, execute: MilestoneExecutor) -> None:
        self._execute = execute

    def run(self, program: Program) -> OrchestratorResult:
        interp = Interpreter(program)
        reply = drive(interp, self._execute)
        return OrchestratorResult(
            reply=reply,
            failed=interp.failed,
            env=dict(interp.env),
            run_log=list(interp.run_log),
        )
