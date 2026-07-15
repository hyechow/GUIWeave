"""Terminal outcomes emitted by statement executors.

``StatementOutcome`` is a *terminal* value only. It may be written to ``run_log`` /
sent into the Program interpreter. Turn-level control remains ``SupervisorStep`` and
must never enter the log as an outcome.

Invariants enforced at construction:

- no ``running`` phase (that would become a second mutable statement state machine)
- ``verification`` only on ``completed``
- ``kickback`` only on ``infeasible``
- impossible bool combos (completed∧failed) cannot be represented
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from gui_agent.core.orchestrator.program import RunResult
from gui_agent.core.schemas import Observation, SupervisorStep

StatementPhase = Literal[
    "completed",
    "failed",
    "exhausted",
    "infeasible",
    "interrupted",
]
Verification = Literal["confirmed", "accepted_unverified"]


def _normalize_details(details: dict[str, Any]) -> dict[str, Any]:
    """Copy collection payloads so a frozen outcome cannot share caller-owned containers."""
    normalized = dict(details)
    for key, factory in (("reads", dict), ("rows", list), ("evidence", list),
                         ("context_reports", list), ("recovery_notices", list)):
        normalized[key] = factory(normalized.get(key) or factory())
    return normalized


@dataclass(frozen=True)
class RecoveryNotice:
    """A recovery event observed by an executor and recorded by the dispatcher."""

    cls: str
    mechanism: str
    site: str
    detail: str = ""
    outcome: str = ""


@dataclass(frozen=True)
class StatementOutcome:
    """Terminal result of exactly one statement (immediate or interactive).

    Use the named constructors. Direct construction validates phase invariants.
    """

    phase: StatementPhase
    summary: str
    verification: Optional[Verification] = None
    kickback: Optional[str] = None
    reads: dict[str, str] = field(default_factory=dict)
    rows: list[dict[str, str]] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    observation: Observation | None = None
    observation_url: str | None = None
    executed_sql: str = ""
    context_reports: list[dict] = field(default_factory=list)
    recovery_notices: list[RecoveryNotice] = field(default_factory=list)
    # Optional structured failure text for recovery promotion (data_query, etc.).
    failure_evidence: str | None = None

    def __post_init__(self) -> None:
        allowed: set[str] = {
            "completed",
            "failed",
            "exhausted",
            "infeasible",
            "interrupted",
        }
        if self.phase not in allowed:
            raise ValueError(
                f"Invalid StatementOutcome.phase={self.phase!r}; "
                f"terminal only, no running. Allowed: {sorted(allowed)}"
            )
        if self.phase == "completed":
            if self.verification not in ("confirmed", "accepted_unverified"):
                raise ValueError(
                    "Completed StatementOutcome requires verification="
                    "confirmed|accepted_unverified"
                )
            if self.kickback:
                raise ValueError("Completed StatementOutcome cannot carry kickback")
        else:
            if self.verification is not None:
                raise ValueError(
                    f"{self.phase} StatementOutcome cannot carry verification"
                )
            if self.phase == "infeasible":
                if not (self.kickback and str(self.kickback).strip()):
                    raise ValueError(
                        "Infeasible StatementOutcome requires a non-empty kickback"
                    )
            elif self.kickback:
                raise ValueError(
                    f"{self.phase} StatementOutcome cannot carry kickback "
                    "(only infeasible may)"
                )

    # ── named constructors ────────────────────────────────────────────

    @classmethod
    def completed(
        cls,
        summary: str,
        *,
        verification: Verification = "confirmed",
        **details: Any,
    ) -> "StatementOutcome":
        return cls(
            phase="completed",
            summary=summary,
            verification=verification,
            **_normalize_details(details),
        )

    @classmethod
    def failed(
        cls,
        summary: str,
        **details: Any,
    ) -> "StatementOutcome":
        details.setdefault("failure_evidence", summary)
        return cls(phase="failed", summary=summary, **_normalize_details(details))

    @classmethod
    def exhausted(
        cls,
        summary: str,
        **details: Any,
    ) -> "StatementOutcome":
        details.setdefault("failure_evidence", summary)
        return cls(phase="exhausted", summary=summary, **_normalize_details(details))

    @classmethod
    def infeasible(
        cls,
        summary: str,
        *,
        kickback: str,
        **details: Any,
    ) -> "StatementOutcome":
        details.setdefault("failure_evidence", summary)
        return cls(
            phase="infeasible",
            summary=summary,
            kickback=kickback,
            **_normalize_details(details),
        )

    @classmethod
    def interrupted(
        cls,
        summary: str,
        **details: Any,
    ) -> "StatementOutcome":
        details.setdefault("failure_evidence", summary)
        return cls(phase="interrupted", summary=summary, **_normalize_details(details))

    # ── projections ───────────────────────────────────────────────────

    @property
    def is_completed(self) -> bool:
        return self.phase == "completed"

    def to_run_result(self) -> RunResult:
        """Project the terminal outcome into the Interpreter's wire type."""
        if self.phase == "completed":
            return RunResult(
                completed=True,
                failed=False,
                completion_status=self.verification or "confirmed",
                reads=dict(self.reads),
                rows=list(self.rows),
                summary=self.summary,
                evidence=list(self.evidence),
            )
        return RunResult(
            completed=False,
            failed=True,
            completion_status="failed",
            reads=dict(self.reads),
            rows=list(self.rows),
            summary=self.summary,
            evidence=list(self.evidence),
        )


def statement_outcome_from_supervisor_step(
    sv_step: SupervisorStep,
    *,
    reads: dict[str, str] | None = None,
    rows: list[dict[str, str]] | None = None,
    notes: list[str] | None = None,
) -> StatementOutcome | None:
    """Map a terminal supervisor step to ``StatementOutcome``.

    Returns ``None`` when the step is still mid-statement (keep looping).
    Task-level success is *not* decided here — only the current statement's terminal
    phase. Callers (ProgramRuntime / loop) own task phase and run_log.
    """
    summary = sv_step.summary or ""
    stop_reason = sv_step.stop_reason or ""
    reason = stop_reason or summary or "statement stopped"
    kickback_text = (sv_step.replan_directive or "").strip()

    if kickback_text:
        return StatementOutcome.infeasible(
            summary or reason,
            kickback=kickback_text,
            evidence=list(notes or []),
        )

    if sv_step.goal_completed:
        verification: Verification = (
            "accepted_unverified"
            if sv_step.completion_status == "accepted_unverified"
            else "confirmed"
        )
        return StatementOutcome.completed(
            summary or reason,
            verification=verification,
            reads=reads,
            rows=rows,
            evidence=list(notes or []),
        )

    if not sv_step.stop:
        return None  # mid-statement: caller should keep driving

    # stop without goal_completed: classify exhausted vs failed vs interrupted
    lowered = reason.lower()
    if "esc" in lowered or "中止" in reason or "interrupt" in lowered:
        return StatementOutcome.interrupted(reason, reads=reads, evidence=list(notes or []))
    if "最大轮数" in reason or "预算" in reason or "exhaust" in lowered:
        return StatementOutcome.exhausted(reason, reads=reads, evidence=list(notes or []))
    return StatementOutcome.failed(
        reason,
        reads=reads,
        evidence=list(notes or []),
        failure_evidence=reason,
    )
