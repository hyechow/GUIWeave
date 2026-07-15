"""Supervisor policy implementations."""

__all__ = ["StatementSupervisorPolicy"]


def __getattr__(name: str):
    if name == "StatementSupervisorPolicy":
        from gui_agent.core.supervisor.statement import StatementSupervisorPolicy
        return StatementSupervisorPolicy
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
