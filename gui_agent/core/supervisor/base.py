"""Supervisor policy interface."""

from typing import Protocol

from gui_agent.core.schemas import JournalEvent, Observation, SupervisorStep


class SupervisorPolicy(Protocol):
    """Supervises one interactive statement at a time."""

    name: str

    def step(
        self,
        observation: Observation,
        goal: str,
        history: list[JournalEvent],
    ) -> SupervisorStep:
        """Given current screen, goal, and full history, decide what to do next."""

    def reconcile(
        self,
        observation: Observation,
        goal: str,
        history: list[JournalEvent],
    ) -> SupervisorStep:
        """Observe and arbitrate once without proposing or dispatching another action."""
