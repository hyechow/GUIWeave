"""DSL orchestrator (MVP): decompose a goal into a small program of milestone-level
run() statements + control flow; a runner interprets it, driving each run() via the
linear (single-milestone) executor and threading structured results through variables.

Boundaries:
  Program Decomposer  user goal -> DSL Program            (decomposer.py — TODO)
  Program Runner      interpret Program; run() -> executor (runner.py)
  Linear Executor     drive ONE milestone -> RunResult     (injected; real driver TODO)
"""

from .program import Cond, Finish, If, Program, Run, RunResult, Stmt
from .runner import (
    Interpreter,
    MilestoneExecutor,
    OrchestratorResult,
    ProgramRunner,
    RunRecord,
    drive,
)

__all__ = [
    "Cond", "Finish", "If", "Program", "Run", "RunResult", "Stmt",
    "Interpreter", "MilestoneExecutor", "OrchestratorResult", "ProgramRunner",
    "RunRecord", "drive",
]
