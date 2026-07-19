"""Result assembly and turn-stat printing for agent runs."""

from __future__ import annotations

import time
from typing import Any, Literal

from llm.structured import get_llm_call_count
from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing_extensions import NotRequired, TypedDict

from gui_agent.core.schemas import (
    PolicyTurn,
    PolicyContext,
    ProgramOutcome,
    ProgramPhase,
    StatementOutcomeEvent,
    StatementPhase,
    Verification,
)
from gui_agent.core.supervisor.base import SupervisorPolicy

TURN_STATS = "\033[2mTurn {turn_no} stats: llm_calls={llm_calls}, elapsed={elapsed:.2f}s\033[0m"

ReportTurnPhase = Literal["running"] | StatementPhase


class AgentTurnDetail(TypedDict):
    """Report-only turn shape; ``running`` is not a runtime terminal phase."""

    no: int
    summary: str
    executed: bool
    phase: ReportTurnPhase
    verification: Verification | None
    action_signal: NotRequired[dict[str, Any]]
    action_type: NotRequired[str]
    action_desc: NotRequired[str]
    not_found: NotRequired[str]


class AgentResult(BaseModel):
    """Frozen external projection of one Program run.

    ProgramOutcome remains the persisted terminal authority. AgentResult adds presentation and
    reporting data, and is dumped to a plain JSON dictionary only at process/API boundaries.
    ``summary`` is the diagnostic terminal conclusion; ``output`` is the user-facing answer
    produced from the run. They intentionally contain the same text for failures created before
    answer synthesis. A chat frontend may persist its later conversational reply as
    ProgramOutcome.output without mutating this execution result.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    goal: str
    output: str
    summary: str
    phase: ProgramPhase
    verification: Verification | None = None
    turns_count: int = 0
    turns_detail: list[AgentTurnDetail] = Field(default_factory=list)
    task_type: str | None = None
    collection_context: str | None = None
    collection_scope: dict[str, Any] | None = None
    orchestrator: dict[str, Any] | None = None
    failure_kind: Literal["compile", "environment"] | None = None

    @model_validator(mode="after")
    def _validate_terminal(self) -> "AgentResult":
        ProgramOutcome(
            phase=self.phase,
            verification=self.verification,
            summary=self.summary,
            output=self.output,
        )
        return self

    def to_program_outcome(self, *, output: str | None = None) -> ProgramOutcome:
        return ProgramOutcome(
            phase=self.phase,
            verification=self.verification,
            summary=self.summary,
            output=self.output if output is None else output,
        )


def failed_result(
    goal: str,
    summary: str,
    *,
    task_type: str | None = None,
    failure_kind: Literal["compile", "environment"] | None = None,
) -> AgentResult:
    """Build a typed pre-runtime failure whose diagnostic is also its only safe output."""
    return AgentResult(
        goal=goal,
        output=summary,
        summary=summary,
        phase="failed",
        task_type=task_type,
        failure_kind=failure_kind,
    )


def make_result(
    context: PolicyContext,
    summary: str,
    collection_context: str | None = None,
    *,
    phase: ProgramPhase = "stopped",
    verification: Verification | None = None,
) -> AgentResult:
    last_summary = summary
    for event in reversed(context.journal.events):
        if isinstance(event, StatementOutcomeEvent):
            last_summary = event.outcome.summary
            break
        if isinstance(event, PolicyTurn):
            last_summary = event.supervisor.summary
            break

    terminal_by_instance = {
        event.statement_instance_id: event.outcome
        for event in context.journal.statement_outcomes
    }
    last_turn_by_instance = {
        turn.statement_instance_id: turn.index
        for turn in context.journal.turns
        if turn.statement_instance_id
    }
    turns_detail: list[AgentTurnDetail] = []
    for t in context.journal.turns:
        terminal = (
            terminal_by_instance.get(t.statement_instance_id)
            if (
                t.statement_instance_id
                and last_turn_by_instance.get(t.statement_instance_id) == t.index
            )
            else None
        )
        turn_phase: ReportTurnPhase = (
            terminal.phase
            if terminal is not None
            else "running"
        )
        turn_verification = (
            terminal.verification
            if terminal is not None
            else None
        )
        entry: AgentTurnDetail = {
            "no": t.index,
            "summary": t.supervisor.summary,
            "executed": t.executed,
            "phase": turn_phase,
            "verification": turn_verification,
        }
        if t.action_signal is not None:
            entry["action_signal"] = t.action_signal.model_dump(mode="json")
        if t.action_decision:
            a = t.action_decision.action
            entry["action_type"] = a.action_type
            entry["action_desc"] = a.description
        turns_detail.append(entry)
    return AgentResult(
        goal=context.goal,
        output=last_summary,
        summary=summary,
        phase=phase,
        verification=verification,
        turns_count=len(context.journal.turns),
        turns_detail=turns_detail,
        task_type=context.task_type,
        collection_context=collection_context,
        collection_scope=(
            context.collection_scope.model_dump(exclude_none=True)
            if context.collection_scope else None
        ),
    )


def orchestration_result(
    context,
    interp,
    terminal: str,
    *,
    current=None,
) -> AgentResult:
    """Build the final result for DSL orchestrator mode."""

    from gui_agent.core.llm.output import compose_orchestration_reply

    report_fields = {
        "phase",
        "summary",
        "verification",
        "kickback",
        "outputs",
        "evidence",
        "observation_url",
        "recovery_notices",
        "failure_evidence",
    }
    run_log = [
        {
            "name": record.name,
            "var": record.var,
            "instance_id": record.instance_id,
            "result": record.result.model_dump(
                mode="json",
                include=report_fields,
                exclude_none=True,
            ),
        }
        for record in interp.run_log
    ]
    digest = [
        {
            "name": r.name,
            "executor": r.executor,
            "phase": r.result.phase,
            "verification": r.result.verification,
            "outputs": dict(r.result.outputs),
            "summary": r.result.summary,
        }
        for r in interp.run_log
    ]
    reply = compose_orchestration_reply(
        context.goal, digest,
        current=(current.goal if current is not None else ""),
        terminal=terminal,
    )
    # A program that reached finish but answered on an entirely-empty read produced no real
    # answer (the read found nothing on the frame) — do not let it masquerade as success.
    finish_incomplete = getattr(interp, "finish_incomplete", False)
    terminal_verification = getattr(interp, "terminal_verification", None)
    accepted_unverified = terminal_verification == "accepted_unverified"
    completed = (
        (current is None)
        and not interp.failed
        and not finish_incomplete
    )
    if completed:
        phase: ProgramPhase = "completed"
        verification: Verification | None = (
            "accepted_unverified" if accepted_unverified else "confirmed"
        )
    elif interp.failed or finish_incomplete:
        phase = "failed"
        verification = None
    elif "ESC" in terminal or "用户退出" in terminal or "用户按" in terminal:
        phase = "interrupted"
        verification = None
    else:
        phase = "stopped"
        verification = None
    base = make_result(
        context,
        terminal,
        phase=phase,
        verification=verification,
    )
    return base.model_copy(update={
        "output": reply,
        "orchestrator": {
            "reply": reply,
            "terminal": terminal,
            "run_log": run_log,
        },
    })


def print_turn_stats(turn_no: int, started_at: float, llm_calls_before: int) -> None:
    elapsed = time.perf_counter() - started_at
    llm_calls = get_llm_call_count() - llm_calls_before
    print(TURN_STATS.format(turn_no=turn_no, llm_calls=llm_calls, elapsed=elapsed))


def print_timings(supervisor: SupervisorPolicy) -> None:
    timings = getattr(supervisor, "_timings", None)
    order = getattr(supervisor, "_timings_order", None)
    if not timings or not order:
        return
    parts = [f"{n}={timings[n]:.2f}s" for n in order]
    total = sum(timings.values())
    print(f"  [Timing] {' | '.join(parts)} | total={total:.2f}s")
