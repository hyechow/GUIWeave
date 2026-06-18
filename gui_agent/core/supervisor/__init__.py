"""Supervisor policy implementations."""

__all__ = ["MilestoneSupervisorPolicy"]


def __getattr__(name: str):
    if name == "MilestoneSupervisorPolicy":
        from gui_agent.core.supervisor.milestone import MilestoneSupervisorPolicy
        return MilestoneSupervisorPolicy
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
