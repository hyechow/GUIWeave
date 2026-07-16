"""LLM-facing semantic transition bridge for one Statement turn."""

from __future__ import annotations

from gui_agent.core.run.statement_memory import StatementMemoryView, build_memory_view
from gui_agent.core.schemas import StatementContract, Observation, PolicyTurn

from .execution_scope import execution_scope_for
from .evidence import resolved_filter_intent
from .model_io import run_statement_transition
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
        evaluation_reason: str = "",
        evaluation_status: str = "",
        evaluation_verification: str = "",
        persistence_summary: str = "",
        extra: str = "",
    ) -> _StatementTransitionResult:
        """Unified LLM decision (Agentic pivot primary path)."""
        memory = memory_view if memory_view is not None else self._memory_view_for(
            statement, history, observation,
        )
        elements = self._elements_knowledge
        if self._pk is not None:
            # Select from deterministic current route/title/statement signals. This is retrieval
            # only, never another LLM or control transition.
            signals = [
                str(observation.title or ""),
                str(observation.url or ""),
                statement.name,
                statement.success_condition,
            ]
            stems = self._pk.match_signals(signals)
            self._last_sections_loaded = stems
            elements = self._pk.bodies(stems)
        runtime_filter = resolved_filter_intent(
            statement,
            observation,
            history,
            scope=execution_scope_for(
                statement,
                observation,
                instance_id=getattr(self, "_active_instance_id", ""),
            ),
        )
        return run_statement_transition(
            statement,
            observation,
            memory_view=memory,
            constraints=list(self._static_constraints),
            extra=extra,
            prompts=self._prompts,
            context_reports=self._context_reports,
            evaluation_reason=evaluation_reason,
            evaluation_status=evaluation_status,
            evaluation_verification=evaluation_verification or "",
            persistence_summary=persistence_summary,
            app_knowledge=self._app_knowledge,
            acceptance_knowledge=self._check_knowledge,
            elements_knowledge=elements,
            initial_filters=self._initial_filters,
            runtime_filter=runtime_filter,
        )


__all__ = ["StatementLLMRuntimeMixin"]
