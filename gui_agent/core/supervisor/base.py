"""Supervisor policy interface."""

from typing import Protocol

from gui_agent.core.schemas import Observation, PolicyTurn, SupervisorStep


class SupervisorPolicy(Protocol):
    """Supervises task execution: observe history, decide whether/what to do next."""

    name: str

    def step(
        self,
        observation: Observation,
        goal: str,
        history: list[PolicyTurn],
    ) -> SupervisorStep:
        """Given current screen, goal, and full history, decide what to do next."""

    def runtime_state_snapshot(self) -> dict:
        """Return supervisor-owned runtime state for persistence."""
        ...
