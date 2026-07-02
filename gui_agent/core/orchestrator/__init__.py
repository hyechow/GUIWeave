"""DSL orchestrator (MVP): decompose a goal into a small program of milestone-level
run() statements + control flow; a runner interprets it, driving each run() via the
linear (single-milestone) executor and threading structured results through variables.

Boundaries:
  Program Decomposer  user goal -> DSL Program            (decomposer.py)
  Program Runner      interpret Program; run() -> executor (runner.py)
  Linear Executor     drive ONE milestone -> RunResult     (injected; real driver TODO)
"""

from .program import (
    Call, Compute, Cond, CondCmp, Finish, ForEach, FunctionDef, If, Program, Run, RunResult, Stmt,
)
from .decomposer import decompose, redecompose, to_program
from .validator import IssueList, ValidationIssue, validate_program
from .preflight import (
    OrchestrationPreflightIssue,
    OrchestrationPreflightResult,
    validate_orchestration_preflight,
)
from .structured_read import structured_read
from .data_query import DataQueryError, execute_data_query
from .budget import estimate_program_turns
from .runner import (
    Interpreter,
    MilestoneExecutor,
    OrchestratorResult,
    ProgramRunner,
    RunRecord,
    drive,
    summarize_progress,
)
from .engine import (
    chain_from_states,
    collapse_foreach_enrichment_passes,
    insert_loop_entry_arrivals,
    normalize_confirm_read_gates,
    normalize_precondition_gates,
)

__all__ = [
    "Call", "Compute", "Cond", "CondCmp", "Finish", "ForEach", "FunctionDef", "If", "Program", "Run", "RunResult", "Stmt",
    "Interpreter", "MilestoneExecutor", "OrchestratorResult", "ProgramRunner",
    "RunRecord", "drive", "summarize_progress", "structured_read", "DataQueryError", "execute_data_query",
    "decompose", "redecompose", "to_program", "validate_program",
    "ValidationIssue", "IssueList",
    "OrchestrationPreflightIssue", "OrchestrationPreflightResult", "validate_orchestration_preflight",
    "estimate_program_turns",
    "normalize_confirm_read_gates", "normalize_precondition_gates", "chain_from_states",
    "collapse_foreach_enrichment_passes", "insert_loop_entry_arrivals",
]
