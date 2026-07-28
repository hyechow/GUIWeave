"""LLM-facing semantic transition bridge for one Statement turn."""

from __future__ import annotations

from gui_agent.core.run.statement_memory import StatementMemoryView, build_memory_view
from gui_agent.core.schemas import StatementContract, Observation, PolicyTurn

from .context_projection import select_transition_knowledge
from .model_io import run_statement_transition
from .observation_view import StatementObservationView, build_observation_view
from .schemas import _StatementTransitionResult


class StatementLLMRuntimeMixin:
    """Prompt/model bridge mixed into ``StatementSupervisorPolicy``."""

    def _memory_view_for(
        self,
        statement: StatementContract,
        history: list[PolicyTurn],
        observation: Observation | None = None,
    ) -> StatementMemoryView:
        """Project the active invocation's Journal turns into decision context."""
        return build_memory_view(
            instance_id=str(getattr(self, "_active_instance_id", "") or ""),
            contract=statement,
            history=history,
            observation=observation,
        )

    def _invoke_statement_transition(
        self,
        statement: StatementContract,
        observation: Observation,
        history: list[PolicyTurn],
        *,
        memory_view: StatementMemoryView | None = None,
        observation_view: StatementObservationView | None = None,
    ) -> _StatementTransitionResult:
        """Unified LLM decision (Agentic pivot primary path)."""
        memory = memory_view if memory_view is not None else self._memory_view_for(
            statement, history, observation,
        )
        observation_view = observation_view or build_observation_view(
            statement, observation, history
        )
        elements = self._elements_knowledge
        if self._pk is not None:
            stems = select_transition_knowledge(
                statement,
                observation,
                self._pk,
            )
            self._last_sections_loaded = stems
            elements = self._pk.bodies(stems)
        return run_statement_transition(
            statement,
            observation,
            memory_view=memory,
            constraints=list(self._static_constraints),
            prompts=self._prompts,
            context_reports=self._context_reports,
            app_knowledge=self._app_knowledge,
            acceptance_knowledge=self._check_knowledge,
            elements_knowledge=elements,
            initial_filters=self._initial_filters,
            observation_view=observation_view,
        )


__all__ = ["StatementLLMRuntimeMixin"]
