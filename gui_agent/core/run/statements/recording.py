"""Reporting and recovery-ledger side effects for immediate statement outcomes."""

from __future__ import annotations

from typing import Any

from llm.structured import get_llm_call_count, get_llm_token_usage

from gui_agent.core.orchestrator.program import RunLike
from gui_agent.core.run.turns import make_immediate_statement_turn
from gui_agent.core.schemas import PolicyContext

from .outcome import StatementOutcome


def record_statement_outcome(
    statement: RunLike,
    outcome: StatementOutcome,
    *,
    statement_index: int,
    context: PolicyContext,
    recovery: Any,
    started_at: float,
    llm_calls_before: int,
    tokens_before: tuple[int, int],
) -> None:
    """Apply reporting and ledger effects after an executor has finished one statement."""
    for notice in outcome.recovery_notices:
        if recovery is not None:
            recovery.record(
                notice.cls,
                notice.mechanism,
                notice.site,
                detail=notice.detail,
                outcome=notice.outcome,
            )

    statement_id = statement.var or f"m{statement_index}_{statement.kind}"
    if not any(item.get("id") == statement_id for item in context.milestones):
        context.milestones.append({
            "id": statement_id,
            "name": statement.name,
            "description": statement.name,
            "kind": statement.kind,
            "success_condition": outcome.summary,
        })

    observation = outcome.observation
    context.turns.append(make_immediate_statement_turn(
        index=len(context.turns) + 1,
        observation_source=(
            getattr(observation, "source", "non_ui")
            if observation is not None
            else "non_ui"
        ),
        milestone_id=statement_id,
        summary=outcome.summary,
        kind=statement.kind,
        name=statement.name,
        var=statement.var or "",
        returns=list(statement.returns),
        read_spec=statement.read_spec,
        sql=outcome.executed_sql,
        data_scope=getattr(statement, "data_scope", "complete"),
        reads=dict(outcome.result.reads),
        completed=outcome.result.completed,
        observation_url=outcome.observation_url or "",
        started_at=started_at,
        llm_calls=get_llm_call_count() - llm_calls_before,
        input_tokens=get_llm_token_usage()[0] - tokens_before[0],
        output_tokens=get_llm_token_usage()[1] - tokens_before[1],
        llm_context=outcome.context_reports,
    ))
