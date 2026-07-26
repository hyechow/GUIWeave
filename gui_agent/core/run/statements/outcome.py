"""Statement terminal outcome contract.

The canonical schema lives with the shared turn schemas so ``SupervisorStep`` can carry
the same object that the coding runtime consumes. This module keeps statement executors'
import path focused on their own package; it defines no compatibility conversion layer.
"""

from gui_agent.core.schemas import (
    RecoveryNotice,
    StatementOutcome,
    StatementPhase,
    Verification,
)

__all__ = [
    "RecoveryNotice",
    "StatementOutcome",
    "StatementPhase",
    "Verification",
]
