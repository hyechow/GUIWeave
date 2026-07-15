"""Terminal statement outcomes and mid-turn executor decisions.

``StatementOutcome`` is a *terminal* value only. It may be written to ``run_log`` /
sent into the Program interpreter. Mid-loop control uses ``ExecutorDecision`` and
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
from gui_agent.core.schemas import Observation

StatementPhase = Literal[
    "completed",
    "failed",
    "exhausted",
    "infeasible",
    "interrupted",
]
Verification = Literal["confirmed", "accepted_unverified"]
ExecutorDecisionKind = Literal["act", "observe", "wait"]


@dataclass(frozen=True)
class RecoveryNotice:
    """A recovery event observed by an executor and recorded by the dispatcher."""

    cls: str
    mechanism: str
    site: str
    detail: str = ""
    outcome: str = ""


@dataclass(frozen=True)
class ExecutorDecision:
    """Mid-turn control signal from an interactive statement executor.

    Never written to ``run_log``. The ProgramRuntime / agent loop acts on it and
    continues the same statement.
    """

    kind: ExecutorDecisionKind
    instruction: str | None = None
    summary: str = ""
    preformed_action: Any = None

    @classmethod
    def act(
        cls,
        instruction: str | None = None,
        *,
        summary: str = "",
        preformed_action: Any = None,
    ) -> "ExecutorDecision":
        return cls(
            kind="act",
            instruction=instruction,
            summary=summary,
            preformed_action=preformed_action,
        )

    @classmethod
    def observe(cls, *, summary: str = "") -> "ExecutorDecision":
        return cls(kind="observe", summary=summary)

    @classmethod
    def wait(cls, *, summary: str = "") -> "ExecutorDecision":
        return cls(kind="wait", summary=summary)


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
        reads: dict[str, str] | None = None,
        rows: list[dict[str, str]] | None = None,
        evidence: list[str] | None = None,
        observation: Observation | None = None,
        observation_url: str | None = None,
        executed_sql: str = "",
        context_reports: list[dict] | None = None,
        recovery_notices: list[RecoveryNotice] | None = None,
    ) -> "StatementOutcome":
        return cls(
            phase="completed",
            summary=summary,
            verification=verification,
            reads=dict(reads or {}),
            rows=list(rows or []),
            evidence=list(evidence or []),
            observation=observation,
            observation_url=observation_url,
            executed_sql=executed_sql,
            context_reports=list(context_reports or []),
            recovery_notices=list(recovery_notices or []),
        )

    @classmethod
    def failed(
        cls,
        summary: str,
        *,
        reads: dict[str, str] | None = None,
        rows: list[dict[str, str]] | None = None,
        evidence: list[str] | None = None,
        observation: Observation | None = None,
        observation_url: str | None = None,
        executed_sql: str = "",
        context_reports: list[dict] | None = None,
        recovery_notices: list[RecoveryNotice] | None = None,
        failure_evidence: str | None = None,
    ) -> "StatementOutcome":
        return cls(
            phase="failed",
            summary=summary,
            reads=dict(reads or {}),
            rows=list(rows or []),
            evidence=list(evidence or []),
            observation=observation,
            observation_url=observation_url,
            executed_sql=executed_sql,
            context_reports=list(context_reports or []),
            recovery_notices=list(recovery_notices or []),
            failure_evidence=failure_evidence if failure_evidence is not None else summary,
        )

    @classmethod
    def exhausted(
        cls,
        summary: str,
        *,
        reads: dict[str, str] | None = None,
        evidence: list[str] | None = None,
        observation: Observation | None = None,
        observation_url: str | None = None,
        recovery_notices: list[RecoveryNotice] | None = None,
        failure_evidence: str | None = None,
    ) -> "StatementOutcome":
        return cls(
            phase="exhausted",
            summary=summary,
            reads=dict(reads or {}),
            evidence=list(evidence or []),
            observation=observation,
            observation_url=observation_url,
            recovery_notices=list(recovery_notices or []),
            failure_evidence=failure_evidence if failure_evidence is not None else summary,
        )

    @classmethod
    def infeasible(
        cls,
        summary: str,
        *,
        kickback: str,
        evidence: list[str] | None = None,
        observation: Observation | None = None,
        observation_url: str | None = None,
        recovery_notices: list[RecoveryNotice] | None = None,
    ) -> "StatementOutcome":
        return cls(
            phase="infeasible",
            summary=summary,
            kickback=kickback,
            evidence=list(evidence or []),
            observation=observation,
            observation_url=observation_url,
            recovery_notices=list(recovery_notices or []),
            failure_evidence=summary,
        )

    @classmethod
    def interrupted(
        cls,
        summary: str,
        *,
        reads: dict[str, str] | None = None,
        evidence: list[str] | None = None,
        observation: Observation | None = None,
        observation_url: str | None = None,
    ) -> "StatementOutcome":
        return cls(
            phase="interrupted",
            summary=summary,
            reads=dict(reads or {}),
            evidence=list(evidence or []),
            observation=observation,
            observation_url=observation_url,
            failure_evidence=summary,
        )

    # ── projections ───────────────────────────────────────────────────

    @property
    def is_completed(self) -> bool:
        return self.phase == "completed"

    @property
    def is_terminal(self) -> bool:
        return True

    def to_run_result(self) -> RunResult:
        """Bridge into the Interpreter's wire type (legacy bools derived, never primary)."""
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

    # Back-compat for call sites still reading ``outcome.result``.
    @property
    def result(self) -> RunResult:
        return self.to_run_result()


def statement_outcome_from_supervisor_step(
    sv_step: Any,
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
    summary = str(getattr(sv_step, "summary", "") or "")
    stop_reason = str(getattr(sv_step, "stop_reason", "") or "")
    reason = stop_reason or summary or "statement stopped"
    kickback = getattr(sv_step, "replan_directive", None)
    kickback_text = str(kickback).strip() if kickback else ""

    completion_status = str(getattr(sv_step, "completion_status", "") or "in_progress")
    goal_completed = bool(getattr(sv_step, "goal_completed", False))
    stop = bool(getattr(sv_step, "stop", False))

    if kickback_text:
        return StatementOutcome.infeasible(
            summary or reason,
            kickback=kickback_text,
            evidence=list(notes or []),
        )

    if goal_completed:
        verification: Verification = (
            "accepted_unverified"
            if completion_status == "accepted_unverified"
            else "confirmed"
        )
        return StatementOutcome.completed(
            summary or reason,
            verification=verification,
            reads=reads,
            rows=rows,
            evidence=list(notes or []),
        )

    if not stop:
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


def executor_decision_from_supervisor_step(sv_step: Any) -> ExecutorDecision | None:
    """Map a non-terminal supervisor step to an ``ExecutorDecision``.

    Returns ``None`` for terminal steps (use ``statement_outcome_from_supervisor_step``).
    """
    if statement_outcome_from_supervisor_step(sv_step) is not None:
        return None
    if bool(getattr(sv_step, "is_loading", False)):
        return ExecutorDecision.wait(summary=str(getattr(sv_step, "summary", "") or "loading"))
    if bool(getattr(sv_step, "should_act", False)):
        return ExecutorDecision.act(
            getattr(sv_step, "instruction", None),
            summary=str(getattr(sv_step, "summary", "") or ""),
            preformed_action=getattr(sv_step, "preformed_action", None),
        )
    return ExecutorDecision.observe(
        summary=str(getattr(sv_step, "summary", "") or ""),
    )
