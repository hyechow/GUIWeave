"""Common outcomes emitted by immediate statement executors."""

from __future__ import annotations

from dataclasses import dataclass, field

from gui_agent.core.orchestrator.runner import RunResult
from gui_agent.core.schemas import Observation


@dataclass(frozen=True)
class RecoveryNotice:
    """A recovery event observed by an executor and recorded by the dispatcher."""

    cls: str
    mechanism: str
    site: str
    detail: str = ""
    outcome: str = ""


@dataclass
class StatementOutcome:
    """Result of executing exactly one statement without entering the Milestone loop."""

    result: RunResult
    summary: str
    observation: Observation | None = None
    observation_url: str | None = None
    executed_sql: str = ""
    context_reports: list[dict] = field(default_factory=list)
    recovery_notices: list[RecoveryNotice] = field(default_factory=list)
    failure_evidence: str | None = None
