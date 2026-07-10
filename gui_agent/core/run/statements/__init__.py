"""Immediate statement executors and their dispatcher."""

from .dispatch import (
    ImmediateDispatchResult,
    drain_immediate_statements,
    is_immediate_statement,
)
from .outcome import RecoveryNotice, StatementOutcome

__all__ = [
    "ImmediateDispatchResult",
    "RecoveryNotice",
    "StatementOutcome",
    "drain_immediate_statements",
    "is_immediate_statement",
]
