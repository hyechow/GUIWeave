"""Program Runner: interpret a DSL Program, driving each run() via the linear executor.

It persists every run's structured RunResult into a variable environment, evaluates
if-conditions against it, and produces the final reply from finish() (or an auto
summary). This is the fix for "the middle read it but the output didn't know": every
milestone's reads/summary live in the env + run_log, not just the last loop's notes.

The linear executor is INJECTED (a callable Run -> RunResult), so the interpreter is
testable with a mock and the real per-milestone driver is wired in later.
"""

from __future__ import annotations

import re
from typing import Callable, Optional

from pydantic import BaseModel, Field

from .program import Cond, Finish, If, Program, Run, RunResult

# A linear executor: given a Run statement (one milestone spec), drive that one
# milestone to a terminal state and return its structured result.
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


class ProgramRunner:
    def __init__(self, execute: MilestoneExecutor) -> None:
        self._execute = execute
        self._env: dict[str, RunResult] = {}
        self._log: list[RunRecord] = []

    def run(self, program: Program) -> OrchestratorResult:
        reply = self._exec_block(program.statements)
        if reply is None:
            reply = self._auto_summary()  # fell off the end without an explicit finish()
        return OrchestratorResult(
            reply=reply,
            failed=any(r.result.failed for r in self._log),
            env=dict(self._env),
            run_log=list(self._log),
        )

    def _exec_block(self, stmts: list) -> Optional[str]:
        """Execute a block. Returns a terminal reply string (finish / failure) or None
        if it ran to the end of the block without terminating."""
        for s in stmts:
            if isinstance(s, Run):
                result = self._execute(s)
                self._log.append(RunRecord(name=s.name, var=s.var, result=result))
                if s.var:
                    self._env[s.var] = result
                if result.failed:
                    # MVP: a failed milestone stops the program and reports honestly.
                    return f"子任务「{s.name}」未能完成：{result.summary or '执行失败'}"
            elif isinstance(s, If):
                branch = s.then if self._eval(s.cond) else s.otherwise
                reply = self._exec_block(branch)
                if reply is not None:
                    return reply
            elif isinstance(s, Finish):
                return self._render(s.message)
        return None

    def _eval(self, cond: Cond) -> bool:
        rv = self._env.get(cond.var)
        actual = (rv.reads.get(cond.field, "") if rv else "").strip()
        target = cond.value.strip()
        return actual == target if cond.cmp == "==" else actual != target

    def _render(self, template: str) -> str:
        def _sub(m: "re.Match[str]") -> str:
            var, field = m.group(1), m.group(2).strip().strip("'\"")
            rv = self._env.get(var)
            return rv.reads.get(field, "") if rv else ""

        return _TEMPLATE_RE.sub(_sub, template)

    def _auto_summary(self) -> str:
        """No explicit finish(): summarize from the persisted run results (reads first,
        else the last run's summary). This is how the orchestrator answers from the
        whole program's structured state, not just the last agent-loop."""
        reads = [
            "；".join(f"{k}：{v}" for k, v in r.result.reads.items() if v)
            for r in self._log
            if r.result.reads
        ]
        reads = [p for p in reads if p]
        if reads:
            return "；".join(reads)
        return (self._log[-1].result.summary if self._log else "") or "任务已执行完毕。"
