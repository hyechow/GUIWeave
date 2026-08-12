"""Stable result contract shared by Tool Agent clients and benchmark harnesses."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from gui_agent.core.schemas import ProgramOutcome, ProgramPhase, Verification


class AgentResult(BaseModel):
    """Frozen external projection of one Tool Agent run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    goal: str
    output: str
    summary: str
    phase: ProgramPhase
    verification: Verification | None = None
    turns_count: int = 0
    turns_detail: list[dict[str, Any]] = Field(default_factory=list)
    task_type: str | None = None
    collection_context: str | None = None
    collection_scope: dict[str, Any] | None = None
    orchestrator: dict[str, Any] | None = None
    failure_kind: Literal["environment", "runtime"] | None = None

    @model_validator(mode="after")
    def _validate_terminal(self) -> "AgentResult":
        self.to_program_outcome()
        return self

    def to_program_outcome(self) -> ProgramOutcome:
        return ProgramOutcome(
            phase=self.phase,
            verification=self.verification,
            summary=self.summary,
            output=self.output,
        )


def failed_result(
    goal: str,
    summary: str,
    *,
    task_type: str | None = None,
    failure_kind: Literal["environment", "runtime"] | None = None,
) -> AgentResult:
    """Build a typed failure whose diagnostic is its only safe output."""

    return AgentResult(
        goal=goal,
        output=summary,
        summary=summary,
        phase="failed",
        task_type=task_type,
        failure_kind=failure_kind,
    )


__all__ = ["AgentResult", "failed_result"]
