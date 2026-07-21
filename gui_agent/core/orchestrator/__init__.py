"""Semantic Program compiler and runtime.

The public surface intentionally mirrors the semantic IR. Concrete UI actions,
observation bindings and pure evaluation remain separate runtime concerns.
"""

from gui_agent.core.schemas import StatementOutcome

from .program import (
    Acquire,
    Command,
    Compute,
    ComputeRef,
    Condition,
    Finish,
    ForEach,
    If,
    Interact,
    ObservationBinding,
    OutputSpec,
    Program,
    Read,
    SourceCheck,
    Stmt,
    ValueRef,
)
from .runner import (
    InputDescriptor,
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
    "Compute",
    "ComputeRef",
    "Condition",
    "Finish",
    "ForEach",
    "If",
    "Interact",
    "ObservationBinding",
    "OutputSpec",
    "Program",
    "Read",
    "SourceCheck",
    "Stmt",
    "ValueRef",
    "StatementOutcome",
    "Interpreter",
    "InputDescriptor",
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
