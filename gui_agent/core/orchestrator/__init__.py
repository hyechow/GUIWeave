"""Semantic Program compiler and runtime.

The public surface intentionally mirrors the semantic IR.  Concrete UI and
data operations live behind runtime executors rather than in this package API.
"""

from gui_agent.core.schemas import StatementOutcome

from .program import (
    Acquire,
    Command,
    Condition,
    Data,
    Finish,
    ForEach,
    If,
    Interact,
    OutputSpec,
    Program,
    Stmt,
    ValueRef,
)
from .runner import (
    Interpreter,
    OrchestratorResult,
    ProgramRunner,
    RunRecord,
    StatementExecutor,
    StatementInvocation,
    summarize_progress,
)
from .decomposer import OrchestratorCompileError, decompose, redecompose, to_program
from .budget import estimate_program_turns
from .validator import IssueList, ValidationIssue, validate_program

__all__ = [
    "Acquire",
    "Command",
    "Condition",
    "Data",
    "Finish",
    "ForEach",
    "If",
    "Interact",
    "OutputSpec",
    "Program",
    "Stmt",
    "ValueRef",
    "StatementOutcome",
    "Interpreter",
    "StatementExecutor",
    "StatementInvocation",
    "OrchestratorResult",
    "ProgramRunner",
    "RunRecord",
    "summarize_progress",
    "OrchestratorCompileError",
    "decompose",
    "redecompose",
    "to_program",
    "IssueList",
    "ValidationIssue",
    "validate_program",
    "estimate_program_turns",
]
